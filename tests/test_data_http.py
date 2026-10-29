"""Private receive-data HTTP controls; fake decoder, no capture or radio access."""
import copy
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


class FakeDecoder:
    def __init__(self, state_dir, hub, *, enabled=False):
        self.hub, self.enabled, self.available = hub, enabled, True
        self.packets = [{'id': 'a'*32, 'time': '2026-10-30T12:00:00Z', 'source': 'TEST1', 'destination': 'APRS',
            'path': ['WIDE1-1'], 'payload': '<script>untrusted packet</script>',
            'raw': 'TEST1>APRS,WIDE1-1:<script>untrusted packet</script>', 'frame_hex': 'aa00'}]
        self.start = Mock(); self.close = Mock(); self.changes = []; self.exports = []; self.limits = []

    def status(self):
        feed = self.hub.raw_input_enabled()
        return {'available': self.available, 'enabled': self.enabled, 'running': self.enabled and feed,
            'input_enabled': feed, 'mode': 'APRS / AX.25 AFSK1200', 'packet_count': len(self.packets),
            'last_packet_at': self.packets[0]['time'] if self.packets else None, 'last_error': None,
            'limits': {'max_packets': 1000, 'max_bytes': 8388608}}

    def list_packets(self, limit=100):
        self.limits.append(limit); return copy.deepcopy(self.packets[:limit])

    def set_enabled(self, enabled):
        self.changes.append(enabled); self.enabled = enabled

    def export(self, format='json'):
        self.exports.append(format)
        if format == 'json':
            return json.dumps({'packets': self.packets}).encode(), 'received-packets.json', 'application/json'
        return (self.packets[0]['raw']+'\n').encode(), 'received-packets.txt', 'text/plain; charset=utf-8'


class DataHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = controls.password_record('receive-data-admin')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        recordings = self.root / 'recordings'; recordings.mkdir()
        self.state = self.root / 'state'; admin = self.root / 'admin.json'
        controls.write_json(admin, self.record)
        with patch.object(dashboard, 'DataDecoder', FakeDecoder), patch.object(dashboard.AudioHub, 'start'), \
             patch.object(dashboard.DtmfControl, 'start', return_value=False):
            self.server = dashboard.make_server('127.0.0.1', 0, recordings, self.state, admin)
        self.decoder = self.server.data_decoder
        self.server.monitor.status = Mock(return_value={'version': dashboard.VERSION, 'setup': {'notes': [], 'rf_enabled': True}})
        self.server.programming.status = Mock(return_value={'pending': False, 'roles': {}, 'pair': None})
        self.server.updates.status = Mock(return_value={'pending': False})
        self.server.wifi.status = Mock(return_value={'pending': False})
        self.server.dtmf.code_status = Mock(return_value={'pending': False})
        self.server.dtmf.status = Mock(return_value={})
        self.server.native_state.read = Mock(return_value=None)
        self.server.messages = SimpleNamespace(active=False, status=Mock(return_value={}))
        token, self.auth = self.server.auth.login('receive-data-admin', '127.0.0.1')
        self.cookie = 'repeater_admin=' + token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3); self.tmp.cleanup()

    def request(self, route, method='GET', data=None, *, auth=False, headers=None):
        supplied = dict(headers or {}); body = None
        if data is not None: body = json.dumps(data).encode(); supplied.setdefault('Content-Type', 'application/json')
        if auth:
            supplied.setdefault('Cookie', self.cookie); supplied.setdefault('X-CSRF-Token', self.auth['csrf_token'])
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, route, body=body, headers=supplied)
        response = connection.getresponse(); result = response.status, dict(response.getheaders()), response.read()
        connection.close(); return result

    def test_status_and_exports_require_admin_including_head_before_decoder_read(self):
        for route in ('/api/data/status', '/api/data/export?format=json', '/api/data/export?format=txt'):
            for method in ('GET', 'HEAD'):
                code, headers, body = self.request(route, method)
                self.assertEqual(code, 403); self.assertIn('no-store', headers['Cache-Control'])
                if method == 'HEAD': self.assertEqual(body, b'')
        self.assertEqual(self.decoder.limits, []); self.assertEqual(self.decoder.exports, [])

    def test_light_status_never_probes_hardware_inventory_and_list_is_bounded(self):
        self.server.data_radio_plan = {'verified': True, 'label': 'Pair 2', 'rx_name': 'BOX-RX2'}
        self.server.monitor.status.side_effect = AssertionError('No system inventory')
        self.server.programming.status.side_effect = AssertionError('No radio inventory')
        self.server.native_state.read.side_effect = AssertionError('No native RF request')
        code, headers, body = self.request('/api/data/status', auth=True)
        self.assertEqual(code, 200); self.assertIn('no-store', headers['Cache-Control'])
        value = json.loads(body)['data']; self.assertEqual(value['rx_name'], 'BOX-RX2'); self.assertEqual(value['pair_label'], 'Pair 2')
        self.assertEqual(value['packets'], self.decoder.packets); self.assertEqual(self.decoder.limits, [100])
        self.server.monitor.status.assert_not_called(); self.server.programming.status.assert_not_called(); self.server.native_state.read.assert_not_called()

    def test_unverified_source_label_is_not_presented_as_current_verified_pair(self):
        self.server.data_radio_plan = {'verified': False, 'label': 'Old pair', 'rx_name': 'OLD-RX'}
        value = json.loads(self.request('/api/data/status', auth=True)[2])['data']
        self.assertIsNone(value['rx_name']); self.assertIsNone(value['pair_label'])

    def test_full_status_hides_packet_history_signed_out_and_returns_it_only_to_admin(self):
        code, _, body = self.request('/api/status'); self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['data'], {'admin_required': True}); self.assertNotIn(b'untrusted packet', body)
        value = json.loads(self.request('/api/status', auth=True)[2])['data']
        self.assertEqual(value['packet_count'], 1); self.assertEqual(value['packets'], self.decoder.packets)

    def test_export_is_fixed_json_or_text_attachment_with_exact_body_and_head(self):
        for format, content_type in (('json', 'application/json'), ('txt', 'text/plain; charset=utf-8')):
            code, headers, body = self.request('/api/data/export?format='+format, auth=True)
            self.assertEqual(code, 200); self.assertEqual(headers['Content-Type'], content_type)
            self.assertIn('attachment;', headers['Content-Disposition']); self.assertIn('received-packets.'+format, headers['Content-Disposition'])
            self.assertEqual(int(headers['Content-Length']), len(body)); self.assertIn(b'untrusted packet', body)
            code, head, no_body = self.request('/api/data/export?format='+format, 'HEAD', auth=True)
            self.assertEqual(code, 200); self.assertEqual(no_body, b''); self.assertEqual(head['Content-Length'], str(len(body)))
        self.assertFalse((self.state / 'message-broadcast-request.json').exists())

    def test_export_rejects_unknown_duplicate_empty_or_path_queries_before_export(self):
        for query in ('', '?format=', '?format=csv', '?format=json&format=txt', '?format=../secret', '?format=json&path=secret', '?format=json&'):
            self.assertEqual(self.request('/api/data/export'+query, auth=True)[0], 400, query)
        self.assertEqual(self.decoder.exports, [])

    def test_toggle_requires_admin_csrf_origin_and_exact_boolean_payload(self):
        route = '/api/data/decoding'
        self.assertEqual(self.request(route, 'POST', {'enabled': True})[0], 403)
        self.assertEqual(self.request(route, 'POST', {'enabled': True}, headers={'Cookie': self.cookie})[0], 403)
        self.assertEqual(self.request(route, 'POST', {'enabled': True}, auth=True, headers={'Origin': 'http://outside.invalid'})[0], 403)
        for payload in ({}, {'enabled': 1}, {'enabled': 'true'}, {'enabled': True, 'path': '/tmp/file'}):
            self.assertEqual(self.request(route, 'POST', payload, auth=True)[0], 400)
        self.assertEqual(self.decoder.changes, []); self.assertFalse(self.server.settings.get()['data_decode_enabled'])

    def test_receive_only_toggle_persists_and_does_not_probe_or_modify_live_rf(self):
        before = self.server.settings.get()
        self.server.programming.status.side_effect = AssertionError('No radio operation or probe')
        self.server.monitor.status.side_effect = AssertionError('No system operation or probe')
        self.server.messages.active = True
        self.server.updates.status.side_effect = AssertionError('No software operation or probe')
        code, _, body = self.request('/api/data/decoding', 'POST', {'enabled': True}, auth=True)
        self.assertEqual(code, 200, body); self.assertEqual(self.decoder.changes, [True])
        state = json.loads(body)['data']; self.assertTrue(state['enabled']); self.assertFalse(state['running']); self.assertFalse(state['input_enabled'])
        saved = controls.SettingsStore(self.state / 'settings.json').get(); self.assertTrue(saved.pop('data_decode_enabled'))
        before.pop('data_decode_enabled'); self.assertEqual(saved, before)
        self.server.programming.status.assert_not_called(); self.server.monitor.status.assert_not_called(); self.server.updates.status.assert_not_called()
        self.assertFalse((self.state / 'message-broadcast-request.json').exists())

    def test_feed_settings_update_syncs_preference_without_duplicating_capture_or_resetting_it(self):
        self.assertFalse(self.server.settings.get()['data_decode_enabled']); self.decoder.start.assert_called_once()
        self.assertEqual(self.request('/api/data/decoding', 'POST', {'enabled': True}, auth=True)[0], 200)
        for enabled in (True, False):
            self.assertEqual(self.request('/api/settings', 'POST', {'input_enabled': enabled}, auth=True)[0], 200)
            value = json.loads(self.request('/api/data/status', auth=True)[2])['data']
            self.assertTrue(value['enabled']); self.assertEqual(value['running'], enabled)
        self.assertEqual(self.decoder.changes, [True, True, True]); self.decoder.start.assert_called_once()
        self.assertTrue(controls.SettingsStore(self.state / 'settings.json').get()['data_decode_enabled'])
        self.assertEqual(self.request('/api/settings', 'POST', {'data_decode_enabled': False}, auth=True)[0], 400)
        for invalid in (0, 'yes', None):
            with self.assertRaises(ValueError): controls.SettingsStore.validate({'data_decode_enabled': invalid})

    def test_close_decoder_precedes_audio_hub_close(self):
        order = []
        self.decoder.close.side_effect = lambda: order.append('decoder')
        actual = self.server.hub.close
        self.server.hub.close = lambda: (order.append('hub'), actual())
        self.server.server_close(); self.assertEqual(order, ['decoder', 'hub'])
        self.decoder.close.side_effect = None


if __name__ == '__main__':
    unittest.main()
