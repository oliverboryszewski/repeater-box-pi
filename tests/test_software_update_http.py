"""Lightweight authenticated updater progress without hardware inventory or RF."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import controls
import server as dashboard
from software_updates import SoftwareUpdates


class SoftwareUpdateStatusHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = controls.password_record('local-test-admin')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'; self.state.mkdir()
        config = self.root / 'updates.json'
        controls.write_json(config, {'repository': 'oliverboryszewski/repeater-box-pi', 'branch': 'main'})
        admin = self.root / 'admin.json'; controls.write_json(admin, self.record)
        self.server = dashboard.DashboardServer(('127.0.0.1', 0), dashboard.Handler)
        self.server.daemon_threads = True
        self.server.auth = controls.AdminAuth(admin)
        self.helper_probe = Mock(return_value='active')
        self.server.updates = SoftwareUpdates(self.state, supported=True, runner=self.helper_probe,
            install_dir=self.root, config_file=config, current_version='0.11.2')
        self.server.monitor = SimpleNamespace(status=Mock(side_effect=AssertionError('No system inventory')))
        self.server.programming = SimpleNamespace(status=Mock(side_effect=AssertionError('No radio/helper inventory')))
        self.server.hub = SimpleNamespace(status=Mock(side_effect=AssertionError('No audio state')), close=lambda: None)
        self.server.messages = SimpleNamespace(status=Mock(side_effect=AssertionError('No message library scan')))
        token, self.session = self.server.auth.login('local-test-admin', '127.0.0.1')
        self.token = token; self.cookie = 'repeater_admin=' + token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3); self.temporary.cleanup()

    def request(self, method='GET', cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, '/api/software-updates/status', headers={'Cookie': cookie} if cookie else {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close(); return result

    def assert_no_heavy_calls(self):
        self.server.monitor.status.assert_not_called(); self.server.programming.status.assert_not_called()
        self.server.hub.status.assert_not_called(); self.server.messages.status.assert_not_called()

    def completed(self, request, version='0.11.3'):
        controls.write_json(self.state / self.server.updates.RESULT, {
            'request_id': request['request_id'], 'action': 'check', 'success': True,
            'current_version': request['current_version'], 'current_commit': request['current_commit'],
            'latest_version': version, 'latest_commit': 'a'*40,
            'checked_at': '2026-10-30T12:00:00+00:00', 'message': 'Dashboard update available.'})

    def test_get_and_head_require_current_admin_session_before_any_update_probe(self):
        for cookie in (None, 'repeater_admin=invalid'):
            for method in ('GET', 'HEAD'):
                code, headers, body = self.request(method, cookie)
                self.assertEqual(code, 403); self.assertIn('no-store', headers['Cache-Control'])
                if method == 'HEAD': self.assertEqual(body, b'')
        self.helper_probe.assert_not_called(); self.assert_no_heavy_calls()

    def test_authorized_get_has_only_update_status_and_never_runs_heavy_inventory(self):
        code, headers, body = self.request(cookie=self.cookie)
        self.assertEqual(code, 200); self.assertIn('no-store', headers['Cache-Control'])
        self.assertEqual(headers['Content-Type'], 'application/json')
        result = json.loads(body); self.assertEqual(set(result), {'software_updates'})
        self.assertTrue(result['software_updates']['available']); self.assertFalse(result['software_updates']['pending'])
        self.helper_probe.assert_called_once_with(['/usr/bin/systemctl', 'is-active', 'repeater-software-update.path'])
        self.assert_no_heavy_calls(); self.assertFalse((self.state / self.server.updates.REQUEST).exists())

    def test_authorized_head_matches_get_headers_without_body_or_mutation(self):
        _, get_headers, get_body = self.request(cookie=self.cookie)
        code, headers, body = self.request('HEAD', self.cookie)
        self.assertEqual(code, 200); self.assertEqual(body, b'')
        self.assertEqual(headers['Content-Length'], str(len(get_body)))
        self.assertEqual(headers['Content-Type'], get_headers['Content-Type']); self.assert_no_heavy_calls()

    def test_expired_or_logged_out_session_stops_progress_reads(self):
        self.server.auth.sessions[self.token]['expires'] = 0
        self.assertEqual(self.request(cookie=self.cookie)[0], 403)
        self.helper_probe.assert_not_called(); self.assert_no_heavy_calls()

    def test_check_pending_poll_and_matching_completion_keep_fresh_install_authority(self):
        first = self.server.updates.request_check(); self.completed(first, '0.11.2')
        self.assertTrue(self.server.updates.status()['can_install'])
        new = self.server.updates.request_check()
        for _ in range(2):
            code, _, body = self.request(cookie=self.cookie); state = json.loads(body)['software_updates']
            self.assertEqual(code, 200); self.assertEqual(state['request_id'], new['request_id']); self.assertTrue(state['pending'])
            self.assertFalse(state['check_stale']); self.assertFalse(state['can_install']); self.assertIsNone(state['latest_version'])
        self.completed(new)
        state = json.loads(self.request(cookie=self.cookie)[2])['software_updates']
        self.assertFalse(state['pending']); self.assertFalse(state['check_stale']); self.assertTrue(state['update_available']); self.assertTrue(state['can_install'])
        self.assertEqual(state['latest_version'], '0.11.3'); self.assert_no_heavy_calls()

    def test_repeated_read_does_not_queue_check_or_install_and_reports_new_worker_failure(self):
        started = self.server.updates.request_check()
        original = (self.state / self.server.updates.REQUEST).read_bytes()
        self.request(cookie=self.cookie)
        controls.write_json(self.state / self.server.updates.RESULT, {'request_id': started['request_id'], 'action': 'check',
            'success': False, 'current_version': '0.11.2', 'current_commit': None,
            'message': 'Cannot reach GitHub. Connect the Pi to the internet, then retry.'})
        state = json.loads(self.request(cookie=self.cookie)[2])['software_updates']
        self.assertFalse(state['pending']); self.assertFalse(state['success']); self.assertFalse(state['can_install'])
        self.assertIn('Cannot reach GitHub', state['message']); self.assertEqual((self.state / self.server.updates.REQUEST).read_bytes(), original)
        self.assert_no_heavy_calls()


if __name__ == '__main__':
    unittest.main()
