"""HTTP admin/CSRF and headerless live-audio-to-archive integration tests."""
from array import array
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import controls
import server as dashboard


class LiveHTTPTest(unittest.TestCase):
    PASSWORD = "portable-local-admin-password"

    @classmethod
    def setUpClass(cls):
        cls.password_record = controls.password_record(cls.PASSWORD)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.state = self.root / "state"
        self.admin_file = self.root / "admin.json"
        controls.write_json(self.admin_file, self.password_record)
        try:
            self.server = dashboard.make_server("127.0.0.1", 0, self.recordings, self.state, self.admin_file)
        except OSError:
            self.tmp.cleanup()
            raise
        # An ephemeral UDP port prevents clashes with a separately running preview.
        self.server.hub.PORT = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def request(self, path, method="GET", data=None, *, authenticated=False, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        supplied = dict(headers or {})
        body = None
        if data is not None:
            supplied.setdefault("Content-Type", "application/json")
            body = json.dumps(data)
        if authenticated:
            supplied.setdefault("Cookie", self.cookie)
            supplied.setdefault("X-CSRF-Token", self.csrf)
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        code, response_headers, payload = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return code, response_headers, payload

    def login(self):
        code, headers, body = self.request("/api/admin/login", "POST", {"password": self.PASSWORD})
        self.assertEqual(code, 200)
        session = json.loads(body)
        self.assertTrue(session["authenticated"])
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = session["csrf_token"]
        return headers

    def test_admin_login_csrf_logout_and_password_reset(self):
        for route, data in (("/api/recording", {"enabled": True}),
                            ("/api/settings", controls.DEFAULTS)):
            self.assertEqual(self.request(route, "POST", data)[0], 403)
        headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        code, _, body = self.request("/api/session", headers={"Cookie": self.cookie})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["csrf_token"], self.csrf)
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": True},
                                      headers={"Cookie": self.cookie})[0], 403)
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": True},
                                      authenticated=True, headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": True},
                                      authenticated=True, headers={"Origin": "http://other.example"})[0], 403)
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": "yes"}, authenticated=True)[0], 400)
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": True}, authenticated=True)[0], 200)
        code, headers, _ = self.request("/api/admin/logout", "POST", {}, authenticated=True)
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": False}, authenticated=True)[0], 403)
        self.login()
        controls.write_json(self.admin_file, controls.password_record(self.PASSWORD))
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": False}, authenticated=True)[0], 403)

    def test_settings_validation_and_persistence(self):
        self.login()
        value = dict(controls.DEFAULTS, station_name="Field Box", retention_days=14,
                     max_archive_mb=512, recording_threshold_dbfs=-40)
        code, _, body = self.request("/api/settings", "POST", value, authenticated=True)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["settings"], value)
        self.assertEqual(controls.SettingsStore(self.state / "settings.json").get(), value)
        self.assertEqual(self.request("/api/settings", "POST", dict(value, retention_days=True), authenticated=True)[0], 400)
        self.assertEqual(self.server.settings.get(), value)
        self.assertEqual(self.server.hub.status()["recorder"]["threshold_dbfs"], -40)

    def test_wifi_visibility_needs_admin_and_csrf_and_queues_only_the_hidden_flag(self):
        route = "/api/wifi-visibility"
        self.assertEqual(self.request(route, "POST", {"hidden": True})[0], 403)
        self.login()
        self.assertEqual(self.request(route, "POST", {"hidden": True}, headers={"Cookie": self.cookie})[0], 403)
        with patch.object(self.server.wifi, "request", return_value={"pending": True, "requested_hidden": True}) as request:
            self.assertEqual(self.request(route, "POST", {"hidden": True, "command": "other"}, authenticated=True)[0], 400)
            request.assert_not_called()
            code, _, body = self.request(route, "POST", {"hidden": True}, authenticated=True)
            self.assertEqual(code, 202)
            self.assertTrue(json.loads(body)["wifi_visibility"]["pending"])
            request.assert_called_once_with(True)

    def test_software_updates_require_admin_csrf_and_narrow_payloads(self):
        check_route = "/api/software-updates/check"
        install_route = "/api/software-updates/install"
        commit = "a" * 40
        self.assertEqual(self.request(check_route, "POST", {})[0], 403)
        self.assertEqual(self.request(install_route, "POST", {"commit": commit})[0], 403)
        self.login()
        for route, body in ((check_route, {}), (install_route, {"commit": commit})):
            self.assertEqual(self.request(route, "POST", body, headers={"Cookie": self.cookie})[0], 403)
            self.assertEqual(self.request(route, "POST", body, authenticated=True,
                                          headers={"Origin": "https://other.example"})[0], 403)
        with patch.object(self.server.updates, "request_check", return_value={"pending": True}) as check:
            self.assertEqual(self.request(check_route, "POST", {"repository": "other/repo"}, authenticated=True)[0], 400)
            check.assert_not_called()
            code, _, body = self.request(check_route, "POST", {}, authenticated=True)
            self.assertEqual(code, 202)
            self.assertTrue(json.loads(body)["software_updates"]["pending"])
            check.assert_called_once_with()
        with patch.object(self.server.updates, "request_install", return_value={"pending": True}) as install:
            self.assertEqual(self.request(install_route, "POST", {"commit": commit, "command": "other"}, authenticated=True)[0], 400)
            install.assert_not_called()
            code, _, body = self.request(install_route, "POST", {"commit": commit}, authenticated=True)
            self.assertEqual(code, 202)
            self.assertTrue(json.loads(body)["software_updates"]["pending"])
            install.assert_called_once_with(commit)

    def test_live_stream_and_recording_toggle_share_one_receiver_feed(self):
        self.assertEqual(self.request("/api/live.pcm")[0], 503)
        self.login()
        enabled_settings = dict(controls.DEFAULTS, input_enabled=True)
        self.assertEqual(self.request("/api/settings", "POST", enabled_settings, authenticated=True)[0], 200)
        self.assertEqual(self.request("/api/recording", "POST", {"enabled": True}, authenticated=True)[0], 200)
        # Listening is deliberately available to devices on the local Wi-Fi;
        # admin authorization is needed only for mutations.
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET", "/api/live.pcm")
        response = connection.getresponse()
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Audio-Format"), "f32le")
            self.assertEqual(response.getheader("X-Sample-Rate"), "48000")
            samples = array("f", [0.125] * 960)
            if sys.byteorder != "little":
                samples.byteswap()
            raw = samples.tobytes()
            self.assertIsNotNone(self.server.hub._socket)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as source:
                source.sendto(raw, self.server.hub._socket.getsockname())
            self.assertEqual(response.read(len(raw)), raw)
            self.assertEqual(list(self.recordings.glob("*.wav")), [])
            code, _, body = self.request("/api/recording", "POST", {"enabled": False}, authenticated=True)
            self.assertEqual(code, 200)
            self.assertFalse(json.loads(body)["recorder"]["active"])
            files = list(self.recordings.glob("rx_*.wav"))
            self.assertEqual(len(files), 1)
            with wave.open(str(files[0]), "rb") as clip:
                self.assertEqual((clip.getframerate(), clip.getnframes()), (48000, 960))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as source:
                source.sendto(raw, self.server.hub._socket.getsockname())
            self.assertEqual(response.read(len(raw)), raw)  # Live continues after recording stops.
            self.assertEqual(self.request("/recordings/" + files[0].name)[2][:4], b"RIFF")
            self.assertEqual(self.request("/api/settings", "POST", controls.DEFAULTS, authenticated=True)[0], 200)
            self.assertEqual(response.read(), b"")  # Disabling the feed closes the stream cleanly.
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
