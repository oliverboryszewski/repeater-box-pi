"""Installer completeness checks use temporary files, never Pi services or RF."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_integrity", ROOT / "scripts/check-install-files.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class InstallIntegrityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.installed = self.root / "installed"
        for name in checker.REQUIRED_SOURCES:
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"Complete fixture for {name}\n".encode())
        for source, target in checker.COPY_FILES:
            path = self.installed / target.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.bundle / source).read_bytes())

    def test_complete_copies_pass_without_comparing_private_configuration(self):
        private = self.installed / "etc/repeater-box/svxlink-bench.conf"
        private.parent.mkdir(parents=True)
        private.write_text("Local operator settings must be preserved.\n")
        checker.check_installed(self.bundle, self.installed)

    def test_source_preflight_rejects_empty_missing_and_nonregular_files(self):
        for name in ("dashboard/static/app.js", "systemd/repeater-engine.service"):
            path = self.bundle / name
            contents = path.read_bytes()
            for kind in ("empty", "missing", "directory", "symlink"):
                with self.subTest(name=name, kind=kind):
                    path.unlink()
                    if kind == "empty":
                        path.touch()
                    elif kind == "directory":
                        path.mkdir()
                    elif kind == "symlink":
                        path.symlink_to(self.bundle / "software-update.json")
                    with self.assertRaises(checker.IntegrityError):
                        checker.check_sources(self.bundle)
                    if kind == "directory":
                        path.rmdir()
                    elif path.exists() or path.is_symlink():
                        path.unlink()
                    path.write_bytes(contents)

    def test_installed_zero_truncated_and_same_size_corruption_are_refused(self):
        for relative in ("opt/repeater-box/dashboard/static/index.html",
                         "etc/systemd/system/repeater-live.service"):
            path = self.installed / relative
            contents = path.read_bytes()
            for damaged in (b"", contents[:-1], b"X" + contents[1:]):
                with self.subTest(path=relative, size=len(damaged)):
                    path.write_bytes(damaged)
                    with self.assertRaises(checker.IntegrityError):
                        checker.check_installed(self.bundle, self.installed)
            path.write_bytes(contents)

    def test_missing_installed_file_and_symlink_are_refused(self):
        path = self.installed / "opt/repeater-box/dashboard/static/app.js"
        path.unlink()
        with self.assertRaises(checker.IntegrityError):
            checker.check_installed(self.bundle, self.installed)
        path.symlink_to(self.bundle / "dashboard/static/app.js")
        with self.assertRaises(checker.IntegrityError):
            checker.check_installed(self.bundle, self.installed)

    def test_cli_preflight_reports_incomplete_source_as_failure(self):
        (self.bundle / "systemd/repeater-dashboard.service").write_bytes(b"")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/check-install-files.py"),
                                 "--bundle", str(self.bundle)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Installation integrity check failed", result.stderr)
        self.assertNotIn("Verified", result.stdout)

    def test_real_bundle_is_complete_and_checks_precede_mutation_and_startup(self):
        checker.check_sources(ROOT)
        source = (ROOT / "scripts/install-pi.sh").read_text()
        preflight = source.index('/usr/bin/python3 "$integrity_checker" --bundle "$bundle_dir"')
        self.assertLess(preflight, source.index("install -d -o root -g root -m 755 /opt/repeater-box"))
        verify = source.index('--bundle "$bundle_dir" --verify-installed')
        flush = source.index("sync -f /opt/repeater-box /etc/systemd/system")
        self.assertLess(source.index('"$bundle_dir/systemd/"{repeater-dtmf-code.service'), verify)
        self.assertLess(verify, flush)
        self.assertLess(flush, source.index("systemctl daemon-reload"))
        self.assertTrue(source.startswith("#!/bin/bash\n"))
        self.assertIn("set -euo pipefail\n", source)

    def test_flush_failure_stops_before_daemon_reload(self):
        source = (ROOT / "scripts/install-pi.sh").read_text()
        commands = source.split("# Detect incomplete copies and flush their filesystems before activating them.\n", 1)[1]
        commands = commands.split("systemctl enable --now avahi-daemon.service", 1)[0]
        tools = self.root / "tools"
        tools.mkdir()
        checker_script = self.root / "passing-checker.py"
        checker_script.write_text("raise SystemExit(0)\n")
        log = self.root / "commands.log"
        for name in ("sync", "systemctl"):
            executable = tools / name
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$COMMAND_LOG"\n'
                                  + ('exit "$SYNC_EXIT"\n' if name == "sync" else "exit 0\n"))
            executable.chmod(0o755)
        environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                           integrity_checker=str(checker_script), bundle_dir=str(self.bundle),
                           COMMAND_LOG=str(log))
        for sync_exit in ("1", "0"):
            with self.subTest(sync_exit=sync_exit):
                log.unlink(missing_ok=True)
                result = subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + commands],
                                        env=dict(environment, SYNC_EXIT=sync_exit), capture_output=True, text=True)
                lines = log.read_text().splitlines()
                self.assertIn("sync -f /opt/repeater-box /etc/systemd/system", lines[0])
                if sync_exit == "1":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(len(lines), 1)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(lines), 2)
                    self.assertTrue(lines[1].endswith("systemctl daemon-reload"))


if __name__ == "__main__":
    unittest.main()
