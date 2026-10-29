"""Offline service/launcher boundaries; never launch SvxLink or radio hardware."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_runner", ROOT / "scripts/run-svxlink.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class LiveServicePlumbingTests(unittest.TestCase):
    def test_live_launch_keeps_fixed_binary_and_filters_rf_digits(self):
        child = mock.Mock()
        child.stdout = iter(("Engine ready\n", "DTMF digit=4\n", "dtmf_cmd_received 123456\n"))
        child.poll.return_value = 0
        child.wait.return_value = 0
        output = io.StringIO()
        with mock.patch.object(runner.subprocess, "Popen", return_value=child) as launch, \
                mock.patch.object(runner.signal, "signal"), contextlib.redirect_stdout(output):
            self.assertEqual(runner.run("/etc/repeater-box/svxlink-live.conf"), 0)
        self.assertEqual(launch.call_args.args[0], ["/usr/bin/svxlink", "--config=/etc/repeater-box/svxlink-live.conf"])
        self.assertEqual(output.getvalue(), "Engine ready\n")
        child.terminate.assert_not_called()

    def test_live_example_and_arbitrary_paths_cannot_launch(self):
        with mock.patch.object(runner.subprocess, "Popen") as launch:
            for path in ("/tmp/svxlink-live.conf", "/etc/repeater-box/svxlink-live.conf.example",
                         "/etc/repeater-box/../repeater-box/svxlink-live.conf"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    runner.run(path)
            launch.assert_not_called()

    def test_bench_and_receive_monitor_conflict_with_live_and_vendor_units(self):
        for name in ("repeater-engine.service", "repeater-rx-monitor.service"):
            unit = (ROOT / "systemd" / name).read_text()
            conflicts = next(line.partition("=")[2].split() for line in unit.splitlines()
                             if line.startswith("Conflicts="))
            with self.subTest(service=name):
                self.assertIn("repeater-live.service", conflicts)
                self.assertIn("svxlink.service", conflicts)
                self.assertIn("/check-bench-config.py ", unit)

    def test_only_requested_signal_exit_is_normalized(self):
        for returncode, expected in ((0, 0), (-signal.SIGTERM, 0),
                                     (-signal.SIGINT, -signal.SIGINT), (3, 3)):
            with self.subTest(returncode=returncode):
                child = mock.Mock()
                handlers = {}
                def register(signum, handler):
                    handlers[signum] = handler
                def output():
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                    yield "Stopping\n"
                child.stdout = output()
                child.poll.side_effect = (None, returncode)
                child.wait.return_value = returncode
                with mock.patch.object(runner.subprocess, "Popen", return_value=child), \
                        mock.patch.object(runner.signal, "signal", side_effect=register), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run("/etc/repeater-box/svxlink-live.conf"), expected)
                child.send_signal.assert_called_once_with(signal.SIGTERM)
                child.terminate.assert_not_called()

    def test_unexpected_child_signal_still_reports_failure(self):
        child = mock.Mock()
        child.stdout = iter(())
        child.wait.return_value = -signal.SIGTERM
        child.poll.return_value = -signal.SIGTERM
        with mock.patch.object(runner.subprocess, "Popen", return_value=child), \
                mock.patch.object(runner.signal, "signal"):
            self.assertEqual(runner.run("/etc/repeater-box/svxlink-live.conf"), -signal.SIGTERM)
        child.send_signal.assert_not_called()

    def test_real_child_receives_one_stop_and_wrapper_exits_successfully(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            child_script = folder / "child.py"
            child_script.write_text('''import signal, time
stopping_at = None
count = 0
def stop(signum, frame):
    global stopping_at, count
    count += 1
    print("CHILD_TERM", count, flush=True)
    if stopping_at is None:
        stopping_at = time.monotonic() + 0.3
signal.signal(signal.SIGTERM, stop)
print("CHILD_READY", flush=True)
while stopping_at is None or time.monotonic() < stopping_at:
    time.sleep(0.01)
''')
            wrapper_script = folder / "wrapper.py"
            wrapper_script.write_text('''import importlib.util, subprocess, sys
spec = importlib.util.spec_from_file_location("runner", sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
real_popen = subprocess.Popen
def launch(arguments, **options):
    assert arguments == ["/usr/bin/svxlink", "--config=/etc/repeater-box/svxlink-live.conf"]
    return real_popen([sys.executable, "-u", sys.argv[2]], **options)
runner.subprocess.Popen = launch
raise SystemExit(runner.run("/etc/repeater-box/svxlink-live.conf"))
''')
            process = subprocess.Popen([sys.executable, str(wrapper_script),
                                        str(ROOT / "scripts/run-svxlink.py"), str(child_script)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(5), "Child did not become ready")
                    self.assertEqual(process.stdout.readline().strip(), "CHILD_READY")
                    process.send_signal(signal.SIGTERM)
                    self.assertTrue(selector.select(5), "Child did not receive TERM")
                    self.assertEqual(process.stdout.readline().strip(), "CHILD_TERM 1")
                    # Repeated requests must not forward a second signal during
                    # the child's graceful shutdown window.
                    process.send_signal(signal.SIGTERM)
                    output, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, errors)
                self.assertNotIn("CHILD_TERM", output)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    process.communicate(timeout=5)

    def test_managed_units_signal_wrapper_then_kill_group_only_on_timeout(self):
        for name in ("repeater-live.service", "repeater-engine.service", "repeater-rx-monitor.service"):
            with self.subTest(unit=name):
                unit = (ROOT / "systemd" / name).read_text()
                self.assertIn("KillMode=mixed\n", unit)
                self.assertIn("/scripts/run-svxlink.py ", unit)
        unit = (ROOT / "systemd/repeater-live.service").read_text()
        self.assertIn("TimeoutStopSec=10\n", unit)
        self.assertIn("Restart=no\n", unit)


if __name__ == "__main__":
    unittest.main()
