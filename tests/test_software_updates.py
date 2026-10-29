"""Updater boundaries and activation recovery without downloading or changing a service."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
from controls import write_json
from software_updates import SoftwareUpdates

SPEC = importlib.util.spec_from_file_location("software_update_worker", ROOT / "scripts/apply-software-update.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
COMMIT = "a" * 40
OLDER = "b" * 40


def payload(version="0.3.0"):
    return {name: (f'VERSION = "{version}"\n'.encode() if name == "dashboard/server.py"
                   else b"VALUE = 1\n" if name.endswith(".py") else b"dashboard asset\n")
            for name in worker.ALLOWED_FILES}


def archive(files=None, manifest=None, extra=()):
    files = payload() if files is None else files
    manifest = {"version": "0.3.0", "update_api": 17} if manifest is None else manifest
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        directory = tarfile.TarInfo("project-sha")
        directory.type = tarfile.DIRTYPE
        stream.addfile(directory)
        for name, data in {**files, "software-update.json": json.dumps(manifest).encode()}.items():
            member = tarfile.TarInfo("project-sha/" + name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
        for member, data in extra:
            stream.addfile(member, io.BytesIO(data) if data is not None else None)
    return output.getvalue()


class SoftwareUpdateControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "updates.json"
        self.config.write_text(json.dumps({"repository": worker.REPOSITORY, "branch": "main"}))
        self.commands = []

    def controller(self, supported=True, active=True):
        def run(arguments):
            self.commands.append(arguments)
            self.assertEqual(arguments, ["/usr/bin/systemctl", "is-active", "repeater-software-update.path"])
            return "active" if active else "inactive"
        return SoftwareUpdates(self.state, supported=supported, runner=run,
                               install_dir=self.root, config_file=self.config, current_version="0.3.0")

    def checked(self, controller):
        result = controller.request_check()
        write_json(self.state / controller.RESULT,
                   {"request_id": result["request_id"], "action": "check", "success": True,
                    "current_version": result["current_version"], "current_commit": result["current_commit"],
                    "latest_version": "0.3.1", "latest_commit": COMMIT,
                    "checked_at": "2026-10-19T12:00:00+00:00", "message": "Available"})
        return controller.status()

    def test_preview_never_queues_or_runs_a_system_command(self):
        controller = self.controller(False)
        self.assertTrue(controller.status()["preview"])
        with self.assertRaises(ValueError):
            controller.request_check()
        self.assertEqual(self.commands, [])
        self.assertFalse((self.state / controller.REQUEST).exists())

    def test_unconfigured_or_inactive_worker_cannot_queue(self):
        with self.assertRaises(ValueError):
            self.controller(active=False).request_check()
        self.config.write_text('{"repository":"attacker/other","branch":"main"}')
        with self.assertRaises(ValueError):
            self.controller().request_check()

    def test_check_is_narrow_and_a_second_request_is_rejected_until_complete(self):
        controller = self.controller()
        state = controller.request_check()
        saved = json.loads((self.state / controller.REQUEST).read_text())
        self.assertEqual(set(saved), {"request_id", "action"})
        self.assertEqual(saved["action"], "check")
        self.assertTrue(state["pending"])
        with self.assertRaises(ValueError):
            controller.request_check()
        with self.assertRaises(ValueError):
            controller.request_install(COMMIT)

    def test_install_requires_a_checked_full_commit_and_cannot_choose_origin(self):
        controller = self.controller()
        with self.assertRaises(ValueError):
            controller.request_install(COMMIT)
        self.assertTrue(self.checked(controller)["update_available"])
        for invalid in ("main", "https://example.com", "a" * 7, "A" * 40, None):
            with self.subTest(commit=invalid), self.assertRaises(ValueError):
                controller.request_install(invalid)
        with self.assertRaises(ValueError):
            controller.request_install(OLDER)
        state = controller.request_install(COMMIT)
        saved = json.loads((self.state / controller.REQUEST).read_text())
        self.assertEqual(set(saved), {"request_id", "action", "commit"})
        self.assertEqual(saved["commit"], COMMIT)
        self.assertEqual(state["action"], "install")

    def test_cached_success_displays_but_new_controller_cannot_authorize_install(self):
        first = self.controller()
        self.checked(first)
        fresh = self.controller()
        state = fresh.status()
        self.assertEqual(state["latest_commit"], COMMIT)
        self.assertTrue(state["update_available"])
        with self.assertRaisesRegex(ValueError, "fresh successful"):
            fresh.request_install(COMMIT)
        self.assertEqual(fresh.status()["action"], "check")

    def test_changed_installed_version_commit_or_check_result_revokes_install_authority(self):
        for change in ("running-version", "installed-version", "installed-commit", "result-version", "result-commit", "result-target", "check-failed"):
            with self.subTest(change=change):
                controller = self.controller()
                self.checked(controller)
                if change == "running-version": controller.current_version = "0.3.2"
                elif change == "installed-version": write_json(self.root / "update-installed.json", {"version": "0.3.2", "commit": None})
                elif change == "installed-commit": write_json(self.root / "update-installed.json", {"version": "0.3.0", "commit": OLDER})
                else:
                    path = self.state / controller.RESULT
                    result = json.loads(path.read_text())
                    if change == "result-version": result["current_version"] = "0.2.0"
                    elif change == "result-commit": result["current_commit"] = OLDER
                    elif change == "result-target": result["latest_commit"] = OLDER
                    else: result["success"] = False
                    write_json(path, result)
                with self.assertRaisesRegex(ValueError, "fresh successful"):
                    controller.request_install(COMMIT)
                (self.root / "update-installed.json").unlink(missing_ok=True)

    def test_new_check_failure_and_install_each_consume_previous_authority(self):
        controller = self.controller()
        self.checked(controller)
        second = controller.request_check()
        write_json(self.state / controller.RESULT, {"request_id": second["request_id"], "action": "check", "success": False})
        with self.assertRaisesRegex(ValueError, "fresh successful"):
            controller.request_install(COMMIT)
        self.checked(controller)
        controller.request_install(COMMIT)
        path = self.state / controller.REQUEST
        old = time.time() - controller.PENDING_SECONDS - 1
        os.utime(path, (old, old))
        with self.assertRaisesRegex(ValueError, "fresh successful"):
            controller.request_install(COMMIT)

    def test_manual_same_version_without_commit_can_seed_provenance_after_fresh_check(self):
        controller = self.controller()
        self.checked(controller)
        path = self.state / controller.RESULT
        result = json.loads(path.read_text())
        result["latest_version"] = "0.3.0"
        # A target change after its first successful observation invalidates it.
        write_json(path, result)
        with self.assertRaises(ValueError): controller.request_install(COMMIT)
        started = controller.request_check()
        result.update(request_id=started["request_id"], current_version="0.3.0", current_commit=None)
        write_json(path, result)
        self.assertIsNone(controller.status()["current_commit"])
        self.assertTrue(controller.request_install(COMMIT)["pending"])

    def test_timeout_is_reported_as_unconfirmed_and_allows_retry(self):
        controller = self.controller()
        state = controller.request_check()
        path = self.state / controller.REQUEST
        old = time.time() - controller.PENDING_SECONDS - 1
        os.utime(path, (old, old))
        status = controller.status()
        self.assertFalse(status["pending"])
        self.assertFalse(status["success"])
        self.assertNotEqual(controller.request_check()["request_id"], state["request_id"])

    def test_matching_result_and_root_metadata_report_success(self):
        controller = self.controller()
        self.checked(controller)
        request = controller.request_install(COMMIT)
        write_json(self.root / "update-installed.json", {"version": "0.3.0", "commit": COMMIT})
        write_json(self.state / controller.RESULT,
                   {"request_id": request["request_id"], "success": True, "latest_version": "0.3.0",
                    "latest_commit": COMMIT, "message": "Installed"})
        status = controller.status()
        self.assertTrue(status["success"])
        self.assertFalse(status["pending"])
        self.assertFalse(status["update_available"])
        self.assertEqual(status["current_commit"], COMMIT)


class SoftwareUpdateWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o755)
        self.live = self.root / "dashboard"
        self.live.mkdir(mode=0o755)
        (self.live / "server.py").write_text('VERSION = "0.2.1"\n')
        (self.live / "old-marker").write_text("previous dashboard")
        self.config = self.root / "updates.json"
        self.config.write_text(json.dumps({"repository": worker.REPOSITORY, "branch": worker.BRANCH}))
        self.fetched = []
        self.actions = []

    def fetch(self, url, maximum):
        self.fetched.append(url)
        if url.endswith("/commits/main"):
            return json.dumps({"sha": COMMIT}).encode()
        self.assertEqual(url, f"https://api.github.com/repos/{worker.REPOSITORY}/tarball/{COMMIT}")
        self.assertEqual(maximum, worker.MAX_ARCHIVE)
        return archive()

    def request(self, action="check", commit=COMMIT):
        value = {"request_id": "1" * 32, "action": action}
        if action == "install":
            value["commit"] = commit
        return value

    def installer(self, health=lambda version: True, run=None):
        def runner(arguments, timeout):
            self.assertEqual(arguments[0], "/usr/bin/systemctl")
            self.assertEqual(arguments[2], "repeater-dashboard.service")
            self.actions.append(arguments[1])
            if run:
                run(arguments, timeout)
        return worker.DashboardInstaller(self.root, runner=runner, health=health, require_root=False)

    def updater(self, fetch=None, installer=None):
        return worker.Updater(self.root, self.config, fetch=fetch or self.fetch,
                              installer=installer or self.installer(), require_root=False)

    def test_worker_request_schema_rejects_commands_paths_urls_and_wrong_types(self):
        path = self.root / "request.json"
        for value in ({"request_id": "1" * 32, "action": "shell", "command": "id"},
                      {**self.request("install"), "repository": "other/repo"},
                      self.request("install", "main"), self.request("install", "../file"),
                      {"request_id": 1, "action": "check"}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    worker.read_request(path)
        path.write_text('{"request_id":"' + "1" * 32 + '","action":"check","action":"install"}')
        with self.assertRaises(ValueError):
            worker.read_request(path)

    def test_request_symlinks_and_oversized_files_are_rejected(self):
        original = self.root / "original.json"
        original.write_text(json.dumps(self.request()))
        link = self.root / "request.json"
        link.symlink_to(original)
        with self.assertRaises(OSError):
            worker.read_request(link)
        link.unlink()
        link.write_text(" " * 2049)
        with self.assertRaises(ValueError):
            worker.read_request(link)

    def test_fixed_https_hosts_and_repository_paths_only(self):
        self.assertTrue(worker.allowed_url(f"https://api.github.com/repos/{worker.REPOSITORY}/commits/main"))
        self.assertTrue(worker.allowed_url(f"https://codeload.github.com/{worker.REPOSITORY}/legacy.tar.gz/{COMMIT}"))
        for url in (f"http://api.github.com/repos/{worker.REPOSITORY}/commits/main",
                    f"https://api.github.com/repos/attacker/other/commits/main",
                    f"https://user:password@api.github.com/repos/{worker.REPOSITORY}/commits/main",
                    f"https://api.github.com:444/repos/{worker.REPOSITORY}/commits/main",
                    f"https://api.github.com/repos/{worker.REPOSITORY}/commits/main?token=secret",
                    f"https://codeload.github.com.evil.example/{worker.REPOSITORY}/tar.gz/{COMMIT}"):
            with self.subTest(url=url):
                self.assertFalse(worker.allowed_url(url))
        with self.assertRaises(worker.UpdateError):
            worker.FixedRedirects().redirect_request(None, None, 302, "Found", {}, "https://example.com/code")

    def test_fetch_enforces_declared_actual_size_and_final_redirect_host(self):
        url = f"https://api.github.com/repos/{worker.REPOSITORY}/commits/main"
        class Response(io.BytesIO):
            def __init__(self, body, headers=None, final=url):
                super().__init__(body)
                self.headers = headers or {}
                self.final = final
            def geturl(self):
                return self.final
        class Opener:
            def __init__(self, response):
                self.response = response
            def open(self, request, timeout):
                return self.response
        self.assertEqual(worker.fetch_bytes(url, 3, opener=Opener(Response(b"abc"))), b"abc")
        for response in (Response(b"four"), Response(b"abc", {"Content-Length": "999"}),
                         Response(b"abc", {"Content-Length": "not-a-size"}),
                         Response(b"abc", final="https://example.com/code")):
            with self.subTest(response=response), self.assertRaises(worker.UpdateError):
                worker.fetch_bytes(url, 3, opener=Opener(response))

    def test_health_uses_lightweight_local_endpoint_and_requires_exact_ready_version(self):
        urls = []
        class Opener:
            def __init__(self, value):
                self.value = value
            def open(self, url, timeout):
                urls.append(url)
                return io.BytesIO(json.dumps(self.value).encode())
        for value, expected in (({"version": "0.3.0", "ready": True}, True),
                                ({"version": "0.3.0", "ready": False}, False),
                                ({"version": "0.2.1", "ready": True}, False)):
            elapsed = [0]
            def pause(seconds):
                elapsed[0] += seconds
            with self.subTest(value=value), patch.object(worker, "build_opener", return_value=Opener(value)):
                self.assertEqual(worker.health_check("0.3.0", timeout=0.6,
                                                     clock=lambda: elapsed[0], pause=pause), expected)
        self.assertEqual(set(urls), {"http://127.0.0.1/api/health"})

    def test_archive_allowlist_ignores_installers_configuration_and_extra_files(self):
        extra = []
        for name in ("scripts/install-pi.sh", "config/svxlink.conf", "etc/admin.json", "README.md"):
            member = tarfile.TarInfo("project-sha/" + name)
            content = b"must never be installed or run"
            member.size = len(content)
            extra.append((member, content))
        selected, manifest = worker.read_archive(archive(extra=extra))
        self.assertEqual(set(selected), set(worker.ALLOWED_FILES))
        self.assertEqual(manifest, {"version": "0.3.0", "update_api": 17})

    def test_archive_traversal_symlinks_hardlinks_devices_duplicates_and_mixed_roots_fail(self):
        for name, kind in (("project-sha/../../etc/passwd", tarfile.REGTYPE),
                           ("/etc/passwd", tarfile.REGTYPE),
                           ("project-sha/link", tarfile.SYMTYPE),
                           ("project-sha/hardlink", tarfile.LNKTYPE),
                           ("project-sha/device", tarfile.CHRTYPE),
                           ("project-sha/dashboard/server.py", tarfile.REGTYPE),
                           ("other-project/README", tarfile.REGTYPE)):
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/etc/passwd"
            member.size = 1 if kind == tarfile.REGTYPE else 0
            with self.subTest(name=name, kind=kind), self.assertRaises(worker.UpdateError):
                worker.read_archive(archive(extra=[(member, b"x" if member.size else None)]))

    def test_missing_files_incompatible_manifest_and_version_mismatch_fail(self):
        missing = payload()
        del missing["dashboard/controls.py"]
        for value in (archive(files=missing), archive(manifest={"version": "0.3.0", "update_api": True}),
                      archive(manifest={"version": "0.3.0", "update_api": 1}),
                      archive(manifest={"version": "0.3.0", "update_api": 2}),
                      archive(manifest={"version": "0.3.0", "update_api": 3}),
                      archive(manifest={"version": "0.3.0", "update_api": 4}),
                      archive(manifest={"version": "0.3.0", "update_api": 5}),
                      archive(manifest={"version": "0.3.0", "update_api": 6}),
                      archive(manifest={"version": "0.3.0", "update_api": 7}),
                      archive(manifest={"version": "0.3.0", "update_api": 8}),
                      archive(manifest={"version": "0.3.0", "update_api": 9}),
                      archive(manifest={"version": "0.3.0", "update_api": 10}),
                      archive(manifest={"version": "0.3.0", "update_api": 11}),
                      archive(manifest={"version": "0.3.0", "update_api": 12}),
                      archive(manifest={"version": "0.3.0", "update_api": 13}),
                      archive(manifest={"version": "0.3.0", "update_api": 14}),
                      archive(manifest={"version": "0.3.0", "update_api": 15}),
                      archive(manifest={"version": "0.3.0", "update_api": 16}),
                      archive(manifest={"version": "0.4.0", "update_api": 17})):
            with self.subTest(size=len(value)), self.assertRaises(worker.UpdateError):
                worker.read_archive(value)

    def test_api8_archive_requires_manual_installer_before_check_or_install_changes(self):
        def fetch(url, maximum):
            if url.endswith("/commits/main"):
                return json.dumps({"sha": COMMIT}).encode()
            return archive(manifest={"version": "0.3.0", "update_api": 8})
        for action in ("check", "install"):
            with self.subTest(action=action):
                state = self.updater(fetch=fetch).apply(self.request(action))
                self.assertFalse(state["success"])
                self.assertIn("Pi installer", state["message"])
                self.assertEqual(self.actions, [])
                self.assertEqual((self.live / "old-marker").read_text(), "previous dashboard")
                self.assertFalse((self.root / "update-installed.json").exists())

    def test_check_downloads_fixed_commit_without_stopping_service_or_installing(self):
        state = self.updater().apply(self.request())
        self.assertTrue(state["success"])
        self.assertTrue(state["update_available"])
        self.assertEqual(state["latest_commit"], COMMIT)
        self.assertEqual(state["latest_version"], "0.3.0")
        self.assertEqual(self.actions, [])
        self.assertTrue((self.live / "old-marker").exists())
        self.assertFalse((self.root / "update-installed.json").exists())

    def test_stale_install_pin_is_refused_before_archive_download_or_service_change(self):
        state = self.updater().apply(self.request("install", OLDER))
        self.assertFalse(state["success"])
        self.assertIn("changed since", state["message"])
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(self.actions, [])

    def test_offline_failure_leaves_live_dashboard_untouched(self):
        def offline(*arguments):
            raise worker.UpdateError("Cannot reach GitHub.")
        state = self.updater(fetch=offline).apply(self.request("install"))
        self.assertFalse(state["success"])
        self.assertEqual(self.actions, [])
        self.assertTrue((self.live / "old-marker").exists())

    def test_already_installed_pin_is_a_no_op_even_if_the_request_is_replayed(self):
        worker.write_json(self.root / "update-installed.json", {"version": "0.3.0", "commit": COMMIT})
        state = self.updater().apply(self.request("install"))
        self.assertTrue(state["success"])
        self.assertFalse(state["update_available"])
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.fetched), 1)

    def test_syntax_failure_occurs_before_service_stop(self):
        invalid = payload()
        invalid["dashboard/controls.py"] = b"def invalid syntax!!!\n"
        with self.assertRaises(worker.UpdateError):
            self.installer().install(invalid, {"version": "0.3.0", "update_api": 17}, COMMIT)
        self.assertEqual(self.actions, [])
        self.assertTrue((self.live / "old-marker").exists())

    def test_success_replaces_only_dashboard_and_preserves_root_state_radio_and_recordings(self):
        protected = {}
        for relative in ("etc/admin.json", "state/settings.json", "recordings/traffic.wav",
                         "config/svxlink.conf", "scripts/guard.py", "systemd/repeater-engine.service"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("preserve " + relative).encode())
            protected[path] = path.read_bytes()
        state = self.updater().apply(self.request("install"))
        self.assertTrue(state["success"], state["message"])
        self.assertEqual(self.actions, ["stop", "start"])
        self.assertEqual(worker.source_version((self.live / "server.py").read_bytes()), "0.3.0")
        self.assertTrue((self.root / "dashboard.previous/old-marker").exists())
        self.assertEqual(json.loads((self.root / "update-installed.json").read_text())["commit"], COMMIT)
        self.assertEqual({path: path.read_bytes() for path in protected}, protected)
        self.assertFalse(list(self.root.glob(".dashboard-update-*")))

    def test_failed_health_rolls_back_previous_files_and_reports_failure(self):
        versions = []
        def health(version):
            versions.append(version)
            return version == "0.2.1"
        state = self.updater(installer=self.installer(health=health)).apply(self.request("install"))
        self.assertFalse(state["success"])
        self.assertIn("restored automatically", state["message"])
        self.assertEqual(self.actions, ["stop", "start", "stop", "start"])
        self.assertEqual(versions, ["0.3.0", "0.2.1"])
        self.assertTrue((self.live / "old-marker").exists())
        self.assertFalse((self.root / "update-installed.json").exists())

    def test_failed_start_rolls_back_without_replacing_radio_service(self):
        failed = False
        def run(arguments, timeout):
            nonlocal failed
            if arguments[1] == "start" and not failed:
                failed = True
                raise worker.UpdateError("Start failed")
        state = self.updater(installer=self.installer(run=run)).apply(self.request("install"))
        self.assertFalse(state["success"])
        self.assertTrue((self.live / "old-marker").exists())
        self.assertIn("restored automatically", state["message"])

    def test_metadata_failure_after_atomic_replace_restores_previous_provenance(self):
        previous = {"version": "0.2.1", "commit": OLDER, "installed_at": "previous"}
        worker.write_json(self.root / "update-installed.json", previous)
        writes = []
        def interrupted_write(path, value):
            worker.write_json(path, value)
            writes.append(value)
            if len(writes) == 1:
                raise OSError("directory fsync failed after replace")
        installer = self.installer()
        installer.write_metadata = interrupted_write
        state = self.updater(installer=installer).apply(self.request("install"))
        self.assertFalse(state["success"])
        self.assertIn("restored automatically", state["message"])
        self.assertEqual(json.loads((self.root / "update-installed.json").read_text()), previous)
        self.assertEqual(writes[0]["commit"], COMMIT)
        self.assertEqual(writes[1], previous)
        self.assertTrue((self.live / "old-marker").exists())

    def test_permanent_metadata_failure_still_restarts_old_dashboard_and_clears_uncertain_history(self):
        writes = []
        def broken_write(path, value):
            writes.append(value)
            # The initial replace succeeds before its simulated fsync error;
            # the later metadata repair cannot write anything at all.
            if len(writes) == 1:
                worker.write_json(path, value)
            raise OSError("filesystem cannot save metadata")
        installer = self.installer()
        installer.write_metadata = broken_write
        state = self.updater(installer=installer).apply(self.request("install"))
        self.assertFalse(state["success"])
        self.assertEqual(self.actions, ["stop", "start", "stop", "start"])
        self.assertTrue((self.live / "old-marker").exists())
        self.assertFalse((self.root / "update-installed.json").exists())
        self.assertIn("restored and restarted", state["message"])
        self.assertIn("history was cleared", state["message"])

    def test_untrusted_or_symlinked_install_directories_are_rejected_before_commands(self):
        self.live.chmod(0o777)
        with self.assertRaises(worker.UpdateError):
            self.installer().install(payload(), {"version": "0.3.0", "update_api": 17}, COMMIT)
        self.assertEqual(self.actions, [])
        self.live.chmod(0o755)
        self.live.rename(self.root / "real-dashboard")
        self.live.symlink_to(self.root / "real-dashboard", target_is_directory=True)
        with self.assertRaises(worker.UpdateError):
            self.installer().install(payload(), {"version": "0.3.0", "update_api": 17}, COMMIT)
        self.assertEqual(self.actions, [])

    def test_root_ledger_deduplicates_requests_independently_of_app_result(self):
        identifier = "1" * 32
        self.assertTrue(worker.mark_consumed(self.root, identifier, require_root=False))
        self.assertFalse(worker.mark_consumed(self.root, identifier, require_root=False))
        self.assertTrue(worker.mark_consumed(self.root, "2" * 32, require_root=False))


if __name__ == "__main__":
    unittest.main()
