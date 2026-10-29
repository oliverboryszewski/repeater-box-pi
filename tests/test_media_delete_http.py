"""Admin-only media deletion over HTTP, using private temporary real stores."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import controls
import server as dashboard


class MediaDeleteHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = controls.password_record('temporary-media-admin')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.recordings = self.root / 'recordings'; self.recordings.mkdir()
        self.state = self.root / 'state'; self.admin = self.root / 'admin.json'
        controls.write_json(self.admin, self.record)
        with patch.object(dashboard.AudioHub, 'start'), patch.object(dashboard.DataDecoder, 'start'), \
             patch.object(dashboard.SstvDecoder, 'start'), patch.object(dashboard.DtmfControl, 'start', return_value=False):
            self.server = dashboard.make_server('127.0.0.1', 0, self.recordings, self.state, self.admin)
        self.server.hub.update_settings({'input_enabled': True})
        self.server.hub.set_recording(True)
        self.server.sstv_decoder.set_enabled(True)
        self.server.monitor.status = Mock(side_effect=AssertionError('No system inventory for media-only requests'))
        self.server.programming.status = Mock(side_effect=AssertionError('No radio programming probe'))
        self.server.updates.status = Mock(side_effect=AssertionError('No update probe'))
        self.server.wifi.status = Mock(side_effect=AssertionError('No Wi-Fi probe'))
        self.server.dtmf.code_status = Mock(side_effect=AssertionError('No DTMF probe'))
        self.server.messages = SimpleNamespace(active=True, status=Mock(side_effect=AssertionError('No transmitter task probe')))
        self.server.native_state.read = Mock(side_effect=AssertionError('No native RF probe'))
        token, self.auth = self.server.auth.login('temporary-media-admin', '127.0.0.1')
        self.cookie = 'repeater_admin=' + token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3); self.tmp.cleanup()

    def request(self, route, method='GET', data=None, *, auth=False, headers=None, raw=None):
        supplied = dict(headers or {}); body = raw
        if data is not None:
            body = json.dumps(data).encode()
        if body is not None:
            supplied.setdefault('Content-Type', 'application/json')
        if auth:
            supplied.setdefault('Cookie', self.cookie); supplied.setdefault('X-CSRF-Token', self.auth['csrf_token'])
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, route, body=body, headers=supplied)
        response = connection.getresponse(); result = response.status, dict(response.getheaders()), response.read()
        connection.close(); return result

    def recording(self, name='saved clip.wav'):
        path = self.recordings / name
        path.write_bytes(b'RIFF private closed recording fixture')
        return path

    def audio_item(self, name):
        code, _, body = self.request('/api/recordings/status', auth=True)
        self.assertEqual(code, 200, body)
        return next(value for value in json.loads(body)['recordings']['files'] if value['id'] == name)

    def audio_payload(self, path):
        value = self.audio_item(path.name)
        return {'id': value['id'], 'revision': value['delete_revision']}

    def picture(self, lines=2):
        decoder = self.server.sstv_decoder
        def accept(kind, **fields):
            self.assertTrue(decoder._accept_output(
                b'SSTV1 ' + json.dumps({'event': kind, **fields}).encode() + b'\n', decoder._epoch))
        accept('begin', mode='Robot36', width=320, height=240)
        for row in range(lines): accept('row', row=row, rgb=(bytes((row, 64, 128)) * 320).hex())
        accept('end', complete=lines == 240)
        value = decoder.list_images()[0]
        return {'id': value['id'], 'revision': value['revision']}

    def assert_no_hardware_probes(self):
        self.server.monitor.status.assert_not_called(); self.server.programming.status.assert_not_called()
        self.server.updates.status.assert_not_called(); self.server.wifi.status.assert_not_called()
        self.server.dtmf.code_status.assert_not_called(); self.server.native_state.read.assert_not_called()
        self.server.messages.status.assert_not_called()

    def test_light_recording_inventory_is_admin_only_and_carries_review_identity(self):
        path = self.recording()
        for method in ('GET', 'HEAD'):
            code, headers, body = self.request('/api/recordings/status', method)
            self.assertEqual(code, 403); self.assertIn('no-store', headers['Cache-Control'])
            if method == 'HEAD': self.assertEqual(body, b'')
        value = self.audio_item(path.name)
        self.assertRegex(value['delete_revision'], r'^[0-9a-f]{64}$')
        self.assertTrue(value['can_delete']); self.assertEqual(value['bytes'], path.stat().st_size)
        code, get_headers, get_body = self.request('/api/recordings/status', auth=True)
        code, head_headers, head_body = self.request('/api/recordings/status', 'HEAD', auth=True)
        self.assertEqual(code, 200); self.assertEqual(head_body, b'')
        self.assertEqual(head_headers['Content-Length'], str(len(get_body)))
        self.assertEqual(head_headers['Content-Type'], get_headers['Content-Type'])
        self.assertEqual(self.request('/api/recordings/status?path=secret', auth=True)[0], 400)
        self.assert_no_hardware_probes()

    def test_public_status_does_not_offer_deletion_fields_but_admin_inventory_does(self):
        path = self.recording()
        def overview():
            files = dashboard.media_files(self.recordings)
            return {'version': dashboard.VERSION, 'setup': {'notes': [], 'rf_enabled': True},
                    'recordings': {'count': len(files), 'total_bytes': sum(value['bytes'] for value in files), 'files': files}}
        self.server.monitor.status = Mock(side_effect=overview)
        self.server.programming.status = Mock(return_value={'pending': False, 'roles': {}, 'pair': None})
        self.server.updates.status = Mock(return_value={'pending': False})
        self.server.wifi.status = Mock(return_value={'pending': False})
        self.server.dtmf.code_status = Mock(return_value={'pending': False})
        self.server.dtmf.status = Mock(return_value={})
        self.server.native_state.read = Mock(return_value=None)
        self.server.messages = SimpleNamespace(active=False, status=Mock(return_value={}))
        code, _, body = self.request('/api/status')
        self.assertEqual(code, 200, body)
        item = json.loads(body)['recordings']['files'][0]
        self.assertEqual(item['id'], path.name)
        self.assertNotIn('delete_revision', item); self.assertNotIn('can_delete', item)
        code, _, body = self.request('/api/status', auth=True)
        self.assertEqual(code, 200, body)
        item = json.loads(body)['recordings']['files'][0]
        self.assertRegex(item['delete_revision'], r'^[0-9a-f]{64}$'); self.assertTrue(item['can_delete'])

    def test_both_delete_routes_require_admin_csrf_and_matching_origin_before_deleting(self):
        path = self.recording(); picture = self.picture()
        audio = self.audio_payload(path)
        for route, payload in (('/api/recordings/delete', audio), ('/api/sstv/delete', picture)):
            for headers in ({}, {'Cookie': self.cookie}, {'Cookie': self.cookie, 'X-CSRF-Token': 'invalid'},
                            {'Cookie': self.cookie, 'X-CSRF-Token': self.auth['csrf_token'], 'Origin': 'http://outside.invalid'}):
                code, _, _ = self.request(route, 'POST', payload, headers=headers)
                self.assertEqual(code, 403, (route, headers))
        self.assertTrue(path.exists()); self.assertEqual(len(self.server.sstv_decoder.list_images()), 1)
        self.assert_no_hardware_probes()

    def test_audio_delete_returns_fresh_inventory_and_preserves_recording_and_live_preferences(self):
        path = self.recording(); other = self.recording('keep.flac')
        payload = self.audio_payload(path); before = self.server.settings.get()
        code, headers, body = self.request('/api/recordings/delete', 'POST', payload, auth=True)
        self.assertEqual(code, 200, body); self.assertIn('no-store', headers['Cache-Control'])
        value = json.loads(body); self.assertFalse(path.exists()); self.assertTrue(other.exists())
        self.assertEqual(value['recordings']['count'], 1)
        self.assertEqual([item['id'] for item in value['recordings']['files']], ['keep.flac'])
        self.assertTrue(value['recorder']['enabled']); self.assertTrue(self.server.hub.raw_input_enabled())
        self.assertEqual(self.server.settings.get(), before); self.assert_no_hardware_probes()

    def test_sstv_delete_removes_matching_pair_cache_and_old_preview_without_resurrection(self):
        payload = self.picture(); decoder = self.server.sstv_decoder
        current = decoder.status()['current']; before = self.server.settings.get()
        code, _, body = self.request('/api/sstv/delete', 'POST', payload, auth=True)
        self.assertEqual(code, 200, body); value = json.loads(body)['sstv']
        self.assertEqual(value['images'], []); self.assertEqual(value['image_count'], 0)
        self.assertIsNone(value['current'])
        self.assertFalse((decoder.directory / (payload['id'] + '.png')).exists())
        self.assertFalse((decoder.directory / (payload['id'] + '.json')).exists())
        url = '/api/sstv/current.png?id=' + current['id'] + '&revision=' + str(current['revision'])
        self.assertEqual(self.request(url, auth=True)[0], 404)
        decoder._end_pipeline(); self.assertEqual(decoder.list_images(), [])
        self.assertEqual(self.server.settings.get(), before); self.assertTrue(decoder.status()['enabled'])
        self.assert_no_hardware_probes()

    def test_exact_json_fields_and_identifier_shapes_are_required(self):
        path = self.recording(); image = self.picture(); audio = self.audio_payload(path)
        for route, payload in (('/api/recordings/delete', audio), ('/api/sstv/delete', image)):
            for bad in ({}, {'id': payload['id']}, {**payload, 'extra': 'unsafe'},
                        {**payload, 'revision': ''}, {**payload, 'revision': 'a' * 63},
                        {**payload, 'revision': 'g' * 64}, {**payload, 'revision': 1},
                        {**payload, 'id': False}, {**payload, 'id': '../outside.wav'}):
                self.assertEqual(self.request(route, 'POST', bad, auth=True)[0], 400, (route, bad))
            duplicate = ('{"id":' + json.dumps(payload['id']) + ',"id":"other","revision":"' + payload['revision'] + '"}').encode()
            self.assertEqual(self.request(route, 'POST', auth=True, raw=duplicate)[0], 400)
        self.assertTrue(path.exists()); self.assertEqual(len(self.server.sstv_decoder.list_images()), 1)

    def test_unsafe_recording_paths_and_in_progress_extension_are_never_deleted(self):
        outside = self.root / 'outside.wav'; outside.write_bytes(b'preserve external file')
        part = self.recordings / 'active.wav.part'; part.write_bytes(b'in progress')
        for identifier in ('../outside.wav', '/tmp/outside.wav', 'folder/file.wav', 'folder\\file.wav',
                           '.hidden.wav', 'active.wav.part', 'nul\0.wav', 'not-audio.txt', 'a' * 256 + '.wav'):
            code, _, _ = self.request('/api/recordings/delete', 'POST', {'id': identifier, 'revision': 'a' * 64}, auth=True)
            self.assertEqual(code, 400, identifier)
        self.assertTrue(outside.exists()); self.assertTrue(part.exists())

    def test_audio_replacement_after_review_is_stale_and_missing_item_is_not_a_success(self):
        path = self.recording(); payload = self.audio_payload(path)
        path.unlink(); path.write_bytes(b'a different recording in the same filename')
        code, _, _ = self.request('/api/recordings/delete', 'POST', payload, auth=True)
        self.assertEqual(code, 409); self.assertTrue(path.exists())
        path.unlink()
        self.assertEqual(self.request('/api/recordings/delete', 'POST', payload, auth=True)[0], 404)

    def test_audio_symlink_or_hardlink_cannot_delete_a_different_file(self):
        path = self.recording(); payload = self.audio_payload(path)
        outside = self.root / 'outside.wav'; outside.write_bytes(b'external recording')
        path.unlink(); path.symlink_to(outside)
        code, _, _ = self.request('/api/recordings/delete', 'POST', payload, auth=True)
        self.assertIn(code, (409, 503)); self.assertTrue(outside.exists()); self.assertTrue(path.is_symlink())
        path.unlink()
        path.hardlink_to(outside)
        code, _, _ = self.request('/api/recordings/delete', 'POST', payload, auth=True)
        self.assertIn(code, (409, 503)); self.assertTrue(outside.exists()); self.assertTrue(path.exists())

    def test_sstv_stale_missing_and_current_receiving_image_are_not_deleted(self):
        image = self.picture(); decoder = self.server.sstv_decoder
        stale = {**image, 'revision': 'f' * 64}
        self.assertEqual(self.request('/api/sstv/delete', 'POST', stale, auth=True)[0], 409)
        unknown = {'id': 'f' * 32, 'revision': 'a' * 64}
        self.assertEqual(self.request('/api/sstv/delete', 'POST', unknown, auth=True)[0], 404)
        with decoder._lock:
            decoder._phase = 'receiving'
        self.assertEqual(self.request('/api/sstv/delete', 'POST', image, auth=True)[0], 409)
        self.assertEqual(len(decoder.list_images()), 1)

    def test_storage_errors_return_recoverable_failure_without_changing_media(self):
        path = self.recording(); audio = self.audio_payload(path); image = self.picture()
        for route, payload, owner, method in (('/api/recordings/delete', audio, self.server.hub, 'delete_recording'),
                                             ('/api/sstv/delete', image, self.server.sstv_decoder, 'delete_image')):
            with patch.object(owner, method, side_effect=OSError('private detail must not leak')):
                code, _, body = self.request(route, 'POST', payload, auth=True)
            self.assertEqual(code, 503); self.assertNotIn(b'private detail', body)
        self.assertTrue(path.exists()); self.assertEqual(len(self.server.sstv_decoder.list_images()), 1)

    def test_second_confirm_cannot_repeat_a_successful_permanent_delete(self):
        path = self.recording(); audio = self.audio_payload(path); image = self.picture()
        for route, payload in (('/api/recordings/delete', audio), ('/api/sstv/delete', image)):
            self.assertEqual(self.request(route, 'POST', payload, auth=True)[0], 200)
            self.assertEqual(self.request(route, 'POST', payload, auth=True)[0], 404)


if __name__ == '__main__':
    unittest.main()
