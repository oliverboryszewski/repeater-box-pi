"""Private SSTV status and PNG routes; no native modem, radio or RF access."""
import copy
import http.client
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import controls
import server as dashboard
from test_data_http import FakeDecoder


def png_bytes():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 320, 240, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes(320 * 3)) * 240)) + chunk(b'IEND', b''))


class FakeSstvDecoder:
    ID = 'a' * 32
    SAVED = 'b' * 32
    PNG = png_bytes()

    def __init__(self, state_dir, hub, *, enabled=False):
        self.hub, self.enabled = hub, enabled
        self.start = Mock(); self.close = Mock()
        self.changes, self.limits, self.image_calls = [], [], []
        self.image_error = None
        self.current = {'id': self.ID, 'revision': 16, 'width': 320, 'height': 240,
            'lines_received': 16, 'started_at': '2026-10-30T12:00:00Z', 'preview_url': 'https://outside.invalid/private'}
        self.saved = [{'id': self.SAVED, 'created_at': '2026-10-30T11:00:00Z', 'mode': 'Robot36',
            'width': 320, 'height': 240, 'lines_received': 240, 'complete': True, 'size_bytes': len(self.PNG),
            'preview_url': 'https://outside.invalid/preview', 'download_url': 'https://outside.invalid/download'}]

    def status(self):
        return {'available': True, 'enabled': self.enabled, 'input_enabled': self.hub.raw_input_enabled(),
            'running': self.enabled and self.hub.raw_input_enabled(), 'mode': 'Robot36', 'phase': 'receiving',
            'progress': 100 * 16 / 240, 'current': copy.deepcopy(self.current), 'image_count': len(self.saved),
            'last_error': None, 'reason': '', 'limits': {'max_images': 50, 'max_bytes': 100 * 1024 * 1024}}

    def list_images(self, limit=50):
        self.limits.append(limit)
        return copy.deepcopy(self.saved[:limit])

    def set_enabled(self, enabled):
        self.changes.append(enabled); self.enabled = enabled

    def image(self, identifier=None, *, revision=None, current_id=None):
        self.image_calls.append((identifier, revision, current_id))
        if self.image_error is not None:
            raise self.image_error
        if identifier is None:
            if revision != self.current['revision'] or current_id != self.current['id']:
                raise ValueError('Stale current image')
            return self.PNG, revision
        if identifier != self.SAVED:
            raise FileNotFoundError('Unknown private image')
        return self.PNG, 240


class SstvHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = controls.password_record('receive-image-admin')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        recordings = self.root / 'recordings'; recordings.mkdir()
        self.state = self.root / 'state'; admin = self.root / 'admin.json'
        controls.write_json(admin, self.record)
        with patch.object(dashboard, 'SstvDecoder', FakeSstvDecoder), patch.object(dashboard, 'DataDecoder', FakeDecoder), \
             patch.object(dashboard.AudioHub, 'start'), patch.object(dashboard.DtmfControl, 'start', return_value=False):
            self.server = dashboard.make_server('127.0.0.1', 0, recordings, self.state, admin)
        self.decoder = self.server.sstv_decoder
        self.server.monitor.status = Mock(return_value={'version': dashboard.VERSION, 'setup': {'notes': [], 'rf_enabled': True}})
        self.server.programming.status = Mock(return_value={'pending': False, 'roles': {}, 'pair': None})
        self.server.updates.status = Mock(return_value={'pending': False})
        self.server.wifi.status = Mock(return_value={'pending': False})
        self.server.dtmf.code_status = Mock(return_value={'pending': False})
        self.server.dtmf.status = Mock(return_value={})
        self.server.native_state.read = Mock(return_value=None)
        self.server.messages = SimpleNamespace(active=False, status=Mock(return_value={}))
        token, self.auth = self.server.auth.login('receive-image-admin', '127.0.0.1')
        self.cookie = 'repeater_admin=' + token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3); self.tmp.cleanup()

    def request(self, route, method='GET', data=None, *, auth=False, headers=None):
        supplied = dict(headers or {}); body = None
        if data is not None:
            body = json.dumps(data).encode(); supplied.setdefault('Content-Type', 'application/json')
        if auth:
            supplied.setdefault('Cookie', self.cookie); supplied.setdefault('X-CSRF-Token', self.auth['csrf_token'])
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, route, body=body, headers=supplied)
        response = connection.getresponse(); result = response.status, dict(response.getheaders()), response.read()
        connection.close(); return result

    @property
    def current_url(self):
        return '/api/sstv/current.png?id=' + self.decoder.ID + '&revision=16'

    def test_sstv_routes_require_admin_before_status_lookup_or_image_read(self):
        for route in ('/api/sstv/status', self.current_url, '/api/sstv/images/' + self.decoder.SAVED + '.png',
                      '/api/sstv/images/' + self.decoder.SAVED + '.png?download=1', '/api/sstv/invalid'):
            for method in ('GET', 'HEAD'):
                code, headers, body = self.request(route, method)
                self.assertEqual(code, 403); self.assertIn('no-store', headers['Cache-Control'])
                if method == 'HEAD': self.assertEqual(body, b'')
        self.assertEqual(self.decoder.limits, []); self.assertEqual(self.decoder.image_calls, [])

    def test_light_status_binds_preview_to_id_revision_and_saved_urls_to_fixed_routes(self):
        self.server.data_radio_plan = {'verified': True, 'label': 'Pair 2', 'rx_name': 'BOX-RX2'}
        self.server.monitor.status.side_effect = AssertionError('No inventory')
        self.server.programming.status.side_effect = AssertionError('No radio')
        self.server.native_state.read.side_effect = AssertionError('No native RF request')
        for route in ('/api/sstv/status', '/api/data/status'):
            code, headers, body = self.request(route, auth=True)
            self.assertEqual(code, 200); self.assertIn('no-store', headers['Cache-Control'])
            value = json.loads(body)['sstv']
            self.assertEqual(value['current']['preview_url'], self.current_url)
            self.assertEqual(value['rx_name'], 'BOX-RX2'); self.assertEqual(value['pair_label'], 'Pair 2')
            image = value['images'][0]
            self.assertEqual(image['preview_url'], '/api/sstv/images/' + self.decoder.SAVED + '.png')
            self.assertEqual(image['download_url'], image['preview_url'] + '?download=1')
            self.assertNotIn(b'outside.invalid', body)
            if route == '/api/data/status': self.assertIn('data', json.loads(body))
        self.assertEqual(self.decoder.limits, [50, 50]); self.assertEqual(self.decoder.image_calls, [])

    def test_full_status_hides_images_signed_out_and_unverified_source_context(self):
        code, _, body = self.request('/api/status'); self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['sstv'], {'admin_required': True}); self.assertNotIn(self.decoder.SAVED.encode(), body)
        value = json.loads(self.request('/api/status', auth=True)[2])['sstv']
        self.assertEqual(value['image_count'], 1); self.assertEqual(value['images'][0]['id'], self.decoder.SAVED)
        self.server.data_radio_plan = {'verified': False, 'label': 'Old pair', 'rx_name': 'OLD-RX'}
        value = json.loads(self.request('/api/sstv/status', auth=True)[2])['sstv']
        self.assertIsNone(value['rx_name']); self.assertIsNone(value['pair_label'])

    def test_live_png_and_gallery_download_are_private_exact_bounded_png_with_head(self):
        for route, disposition in ((self.current_url, 'inline'),
                ('/api/sstv/images/' + self.decoder.SAVED + '.png', 'inline'),
                ('/api/sstv/images/' + self.decoder.SAVED + '.png?download=1', 'attachment')):
            code, headers, body = self.request(route, auth=True)
            self.assertEqual(code, 200); self.assertEqual(body, self.decoder.PNG)
            self.assertEqual(headers['Content-Type'], 'image/png'); self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn('no-store', headers['Cache-Control']); self.assertEqual(int(headers['Content-Length']), len(body))
            self.assertTrue(headers['Content-Disposition'].startswith(disposition + ';'))
            self.assertNotIn('filename=', headers['Content-Disposition'])
            code, head, no_body = self.request(route, 'HEAD', auth=True)
            self.assertEqual(code, 200); self.assertEqual(no_body, b''); self.assertEqual(head['Content-Length'], str(len(body)))
        self.assertEqual(self.decoder.image_calls[0], (None, 16, self.decoder.ID))
        self.assertEqual(self.decoder.image_calls[-1], (self.decoder.SAVED, None, None))

    def test_png_queries_reject_unknown_duplicate_oversized_and_noncanonical_fields(self):
        for query in ('', '?revision=16', '?id=' + self.decoder.ID, '?id=../secret&revision=16',
                      '?id=' + self.decoder.ID + '&revision=0', '?id=' + self.decoder.ID + '&revision=2147483648',
                      '?id=' + self.decoder.ID + '&revision=16&revision=17',
                      '?id=' + self.decoder.ID + '&revision=16&other=1',
                      '?id=' + self.decoder.ID + '&revision=016', '?id=' + 'a' * 97):
            self.assertEqual(self.request('/api/sstv/current.png' + query, auth=True)[0], 400, query)
        for query in ('?download=', '?download=0', '?download=1&download=1', '?path=/etc/passwd', '?download=1&'):
            self.assertEqual(self.request('/api/sstv/images/' + self.decoder.SAVED + '.png' + query, auth=True)[0], 400, query)
        self.assertEqual(self.request('/api/sstv/status?extra=1', auth=True)[0], 400)
        self.assertEqual(self.decoder.image_calls, [])

    def test_stale_current_id_or_revision_cannot_serve_a_different_picture(self):
        for route in (self.current_url.replace('revision=16', 'revision=15'),
                      self.current_url.replace(self.decoder.ID, 'c' * 32)):
            self.assertEqual(self.request(route, auth=True)[0], 409)
        self.assertEqual(self.request('/api/sstv/images/' + 'c' * 32 + '.png', auth=True)[0], 404)
        for route in ('/api/sstv/images/../../admin.json', '/api/sstv/images/../../admin.json.png',
                      '/api/sstv/images/' + 'A' * 32 + '.png', '/api/sstv/images/' + self.decoder.SAVED + '.svg'):
            self.assertEqual(self.request(route, auth=True)[0], 404)

    def test_image_store_failure_is_safe_and_never_sends_non_png_or_oversized_payload(self):
        self.decoder.image_error = OSError('private detail must not escape')
        code, _, body = self.request(self.current_url, auth=True)
        self.assertEqual(code, 503); self.assertNotIn(b'private detail', body)
        self.decoder.image_error = None
        for value in ((b'<svg>unsafe</svg>' * 4, 16), (self.decoder.PNG, 'injected'),
                      (b'\x89PNG\r\n\x1a\n' + bytes(2 * 1024 * 1024), 16), (self.decoder.PNG, 17)):
            with patch.object(self.decoder, 'image', return_value=value):
                self.assertEqual(self.request(self.current_url, auth=True)[0], 503)

    def test_toggle_requires_admin_csrf_origin_and_exact_boolean_payload(self):
        route = '/api/sstv/decoding'
        self.assertEqual(self.request(route, 'POST', {'enabled': True})[0], 403)
        self.assertEqual(self.request(route, 'POST', {'enabled': True}, headers={'Cookie': self.cookie})[0], 403)
        self.assertEqual(self.request(route, 'POST', {'enabled': True}, auth=True, headers={'Origin': 'http://outside.invalid'})[0], 403)
        for payload in ({}, {'enabled': 1}, {'enabled': 'true'}, {'enabled': True, 'file': '/tmp/image.png'}):
            self.assertEqual(self.request(route, 'POST', payload, auth=True)[0], 400)
        self.assertEqual(self.decoder.changes, []); self.assertFalse(self.server.settings.get()['sstv_decode_enabled'])

    def test_receive_only_toggle_persists_without_radio_probes_or_affecting_aprs_or_recording(self):
        before = self.server.settings.get()
        self.server.monitor.status.side_effect = AssertionError('No inventory')
        self.server.programming.status.side_effect = AssertionError('No radio')
        self.server.updates.status.side_effect = AssertionError('No maintenance')
        self.server.messages.active = True
        self.server.data_decoder.set_enabled = Mock(side_effect=AssertionError('No APRS change'))
        self.server.hub.set_recording = Mock(side_effect=AssertionError('No recording change'))
        code, _, body = self.request('/api/sstv/decoding', 'POST', {'enabled': True}, auth=True)
        self.assertEqual(code, 200, body); self.assertEqual(self.decoder.changes, [True])
        state = json.loads(body)['sstv']; self.assertTrue(state['enabled']); self.assertFalse(state['input_enabled'])
        saved = controls.SettingsStore(self.state / 'settings.json').get(); self.assertTrue(saved.pop('sstv_decode_enabled'))
        before.pop('sstv_decode_enabled'); self.assertEqual(saved, before)
        self.server.programming.status.assert_not_called(); self.server.monitor.status.assert_not_called(); self.server.updates.status.assert_not_called()
        self.assertFalse((self.state / 'message-broadcast-request.json').exists())

    def test_feed_settings_syncs_sstv_preference_and_requires_dedicated_toggle(self):
        self.decoder.start.assert_called_once()
        self.assertEqual(self.request('/api/sstv/decoding', 'POST', {'enabled': True}, auth=True)[0], 200)
        for enabled in (True, False):
            self.assertEqual(self.request('/api/settings', 'POST', {'input_enabled': enabled}, auth=True)[0], 200)
            value = json.loads(self.request('/api/sstv/status', auth=True)[2])['sstv']
            self.assertTrue(value['enabled']); self.assertEqual(value['running'], enabled)
        self.assertEqual(self.decoder.changes, [True, True, True]); self.decoder.start.assert_called_once()
        self.assertEqual(self.request('/api/settings', 'POST', {'sstv_decode_enabled': False}, auth=True)[0], 400)
        for invalid in (0, 'yes', None):
            with self.assertRaises(ValueError): controls.SettingsStore.validate({'sstv_decode_enabled': invalid})

    def test_close_sstv_before_raw_audio_hub_and_initial_default_is_off(self):
        self.assertFalse(controls.DEFAULTS['sstv_decode_enabled'])
        order = []; self.decoder.close.side_effect = lambda: order.append('sstv')
        self.server.data_decoder.close.side_effect = lambda: order.append('aprs')
        actual = self.server.hub.close
        self.server.hub.close = lambda: (order.append('hub'), actual())
        self.server.server_close(); self.assertEqual(order, ['sstv', 'aprs', 'hub'])
        self.decoder.close.side_effect = None; self.server.data_decoder.close.side_effect = None


if __name__ == '__main__':
    unittest.main()
