"""Run with python3 -m unittest discover -s tests -v from the bundle directory."""
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
spec = importlib.util.spec_from_file_location("dashboard", ROOT / "dashboard/server.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class USBInventoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def device(self, name, vendor=None, product_id=None, product=None):
        folder = self.root / name
        folder.mkdir()
        for field, value in (("idVendor", vendor), ("idProduct", product_id), ("product", product)):
            if value is not None:
                (folder / field).write_text(value + "\n")
        return folder

    def test_standard_aiocs_count_by_ids_with_normal_or_missing_product_names(self):
        self.device("1-1", "1209", "7388", "All-In-One-Cable")
        self.device("1-2", "1209", "7388")
        self.device("usb1", "1d6b", "0002", "USB root hub")
        self.device("1-1:1.0", "1209", "7388", "All-In-One-Cable")
        self.assertEqual(dashboard.count_aioc_usb_devices(self.root), 2)

    def test_product_fallback_is_exact_and_cannot_override_different_usb_ids(self):
        self.device("1-1", product="All-In-One-Cable")
        self.device("1-2", product="AIOC")
        self.device("1-3", "ffff", "7388", "All-In-One-Cable")
        self.device("1-4", "1209", "ffff", "AIOC")
        self.device("1-5", product="Unrelated AIOC-compatible adapter")
        self.device("1-6", "ffff", product="All-In-One-Cable")
        self.assertEqual(dashboard.count_aioc_usb_devices(self.root), 2)

    def test_unavailable_usb_inventory_is_unknown_and_empty_inventory_is_zero(self):
        self.assertIsNone(dashboard.count_aioc_usb_devices(self.root / "missing"))
        self.assertEqual(dashboard.count_aioc_usb_devices(self.root), 0)

    def test_linux_status_exposes_the_physical_inventory_count(self):
        with mock.patch.object(dashboard.platform, "system", return_value="Linux"), \
                mock.patch.object(dashboard, "read_text", return_value=None), \
                mock.patch.object(dashboard, "command", return_value=None), \
                mock.patch.object(dashboard, "count_aioc_usb_devices", return_value=2) as inventory:
            status = dashboard.Monitor(self.root).status()
        inventory.assert_called_once_with()
        self.assertEqual(status["audio"]["usb_aioc_count"], 2)


class EngineModeTest(unittest.TestCase):
    def status(self, live="inactive", rx="inactive", bench="inactive"):
        states = {"repeater-live.service": live, "repeater-rx-monitor.service": rx,
                  "repeater-engine.service": bench}
        def read_command(args):
            return states.get(args[2]) if args[:2] == ["systemctl", "show"] else None
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(dashboard.platform, "system", return_value="Linux"), \
                mock.patch.object(dashboard, "read_text", return_value=None), \
                mock.patch.object(dashboard, "command", side_effect=read_command), \
                mock.patch.object(dashboard, "count_aioc_usb_devices", return_value=2):
            return dashboard.Monitor(temporary).status()

    def test_active_live_service_reports_rf_enabled_without_claiming_ptt_telemetry(self):
        status = self.status(live="active")
        self.assertEqual(status["svxlink"]["mode"], "repeater")
        self.assertEqual(status["svxlink"]["state"], "active")
        self.assertEqual(status["svxlink"]["transmit_control"], "enabled")
        self.assertIsNone(status["svxlink"]["transmitting"])
        self.assertEqual(status["setup"]["phase"], "Live repeater")
        self.assertTrue(status["setup"]["rf_enabled"])

    def test_inactive_failed_or_starting_live_service_does_not_claim_rf_active(self):
        for state in ("inactive", "failed", "activating", None):
            with self.subTest(state=state):
                status = self.status(live=state, rx="active")
                self.assertEqual(status["svxlink"]["mode"], "receive_only")
                self.assertEqual(status["svxlink"]["transmit_control"], "disabled")
                self.assertFalse(status["setup"]["rf_enabled"])


class DashboardHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.recordings = Path(cls.tmp.name) / "recordings"
        cls.recordings.mkdir()
        (cls.recordings / "test clip.wav").write_bytes(b"RIFF0123456789")
        outside = Path(cls.tmp.name) / "secret.wav"
        outside.write_bytes(b"private")
        (cls.recordings / "linked.wav").symlink_to(outside)
        (cls.recordings / "notes.txt").write_text("not an audio file")
        cls.server = dashboard.make_server("127.0.0.1", 0, cls.recordings)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_status_reports_honest_bench_and_recordings(self):
        code, headers, body = self.request("/api/status")
        value = json.loads(body)
        self.assertEqual(code, 200)
        self.assertFalse(value["setup"]["rf_enabled"])
        self.assertEqual(value["setup"]["phase"], "bench")
        self.assertEqual(value["recordings"]["count"], 1)
        self.assertEqual(value["recordings"]["files"][0]["name"], "test clip.wav")
        self.assertIn("no-store", headers["Cache-Control"])
        if not value["is_pi"]:
            self.assertIsNone(value["cpu_temp_c"])
            self.assertFalse(value["power"]["available"])

    def test_health_check_does_not_depend_on_hardware_inventory(self):
        from unittest.mock import patch
        with patch.object(self.server.monitor, "status", side_effect=AssertionError("Hardware inventory must not run")):
            code, headers, body = self.request("/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"version": dashboard.VERSION, "ready": True})
        self.assertIn("no-store", headers["Cache-Control"])

    def test_health_refuses_missing_empty_and_nonregular_interface_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            static = Path(temporary)
            for name in set(dashboard.STATIC_ROUTES.values()):
                (static / name).write_bytes(b"complete interface asset\n")
            with mock.patch.object(dashboard, "STATIC", static), \
                    mock.patch.object(self.server.monitor, "status", side_effect=AssertionError("No hardware check")):
                self.assertEqual(self.request("/api/health")[0], 200)
                for name in ("index.html", "app.js", "radio-editor.js"):
                    path = static / name
                    for kind in ("empty", "missing", "directory"):
                        with self.subTest(name=name, kind=kind):
                            path.unlink()
                            if kind == "empty":
                                path.touch()
                            elif kind == "directory":
                                path.mkdir()
                            code, _, body = self.request("/api/health")
                            self.assertEqual(code, 503)
                            self.assertEqual(json.loads(body), {"version": dashboard.VERSION, "ready": False})
                            if kind == "directory":
                                path.rmdir()
                            elif path.exists():
                                path.unlink()
                            path.write_bytes(b"complete interface asset\n")
                self.assertEqual(self.request("/api/health")[0], 200)

    def test_media_full_and_byte_ranges_for_mobile_players(self):
        path = "/recordings/test%20clip.wav"
        code, _, body = self.request(path)
        self.assertEqual((code, body), (200, b"RIFF0123456789"))
        code, headers, body = self.request(path, headers={"Range": "bytes=4-7"})
        self.assertEqual((code, body), (206, b"0123"))
        self.assertEqual(headers["Content-Range"], "bytes 4-7/14")
        code, _, body = self.request(path, headers={"Range": "bytes=-3"})
        self.assertEqual((code, body), (206, b"789"))
        code, headers, _ = self.request(path, headers={"Range": "bytes=999-"})
        self.assertEqual(code, 416)
        self.assertEqual(headers["Content-Range"], "bytes */14")
        code, headers, body = self.request(path, "HEAD", {"Range": "bytes=0-3"})
        self.assertEqual((code, body), (206, b""))
        self.assertEqual(headers["Content-Length"], "4")

    def test_no_filesystem_escape_or_mutation_routes(self):
        for path in ("/recordings/linked.wav", "/recordings/notes.txt", "/recordings/%2e%2e%2fsecret.wav", "/../server.py", "/recordings/%00.wav"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/transmit", "POST", {"Content-Type": "application/json"})[0], 400)

    def test_static_assets_are_served_with_browser_policy(self):
        for path in ("/", "/app.js", "/style.css"):
            code, headers, body = self.request(path)
            self.assertEqual(code, 200)
            self.assertGreater(len(body), 100)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])


class BenchGuardsTest(unittest.TestCase):
    def run_guard(self, path):
        return subprocess.run([sys.executable, str(ROOT / "scripts/check-bench-config.py"), str(path)], capture_output=True, timeout=5)

    def test_shipped_bench_passes_and_live_template_fails(self):
        self.assertEqual(self.run_guard(ROOT / "config/svxlink-bench.conf").returncode, 0)
        self.assertNotEqual(self.run_guard(ROOT / "config/svxlink-live.conf.example").returncode, 0)

    def test_guard_rejects_any_active_logic_with_radio_hardware(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "bench.conf"
            file.write_text("[GLOBAL]\nLOGICS=Bench,Second\n[Bench]\nRX=NONE\nTX=NONE\nONLINE=0\n[Second]\nRX=NONE\nTX=TxRadio\nONLINE=0\n")
            self.assertNotEqual(self.run_guard(file).returncode, 0)

    def test_receive_monitor_requires_real_mapping_and_keeps_transmitter_disabled(self):
        guard = str(ROOT / "scripts/check-bench-config.py")
        template = (ROOT / "config/svxlink-rx-monitor.conf.example").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "monitor.conf"
            def check(text):
                file.write_text(text)
                return subprocess.run([sys.executable, guard, str(file), "--rx-monitor"], capture_output=True, timeout=5).returncode
            self.assertNotEqual(check(template), 0)
            mapped = template.replace("REPLACE_RX_ALSA_ID", "ObservedRX")
            self.assertEqual(check(mapped), 0)
            self.assertNotEqual(check(mapped.replace("TX=NONE", "TX=TxAIOC")), 0)
            self.assertNotEqual(check(mapped.replace("48000", "16000")), 0)


if __name__ == "__main__":
    unittest.main()
