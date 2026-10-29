"""Offline service/launcher boundaries; never launch SvxLink or radio hardware."""
import contextlib
import importlib.util
import io
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
