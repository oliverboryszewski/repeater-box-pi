"""Deterministic RX audio/recording checks; no radio or ALSA device is opened."""
from array import array
import importlib.util
import os
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_hub", ROOT / "dashboard/audio_hub.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
AudioHub = module.AudioHub


def packet(value, frames=960):
    samples = array("f", [value] * frames)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class AudioHubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 0.0
        self.wall = 1700000000.0
        self.settings = {"input_enabled": True, "record_on_startup": False,
                         "retention_days": 7, "max_archive_mb": 2048,
                         "recording_threshold_dbfs": -45}
        free = mock.Mock(free=1024 * 1024 * 1024)
        self.space = mock.Mock(return_value=free)
        self.hub = AudioHub(self.root, self.settings, monotonic=lambda: self.now,
                            wall_time=lambda: self.wall, disk_usage=self.space)

    def tearDown(self):
        self.hub.close()
        self.tmp.cleanup()

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds

    def clips(self):
        return sorted(self.root.glob("rx_*.wav"))

    def closed_clip(self, identifier, size=32 * 1024 * 1024, age=100):
        path = self.root / f"rx_20230101_000000_{identifier * 12}.wav"
        with path.open("wb") as stream:
            stream.truncate(size)
        os.utime(path, (self.wall - age, self.wall - age))
        return path

    def test_live_listening_is_independent_from_recording_and_has_bounded_delay(self):
        key, listener = self.hub.subscribe()
        second_key, second = self.hub.subscribe()
        for _ in range(40):
            self.hub.feed_packet(packet(0.1))
        self.assertEqual(listener.qsize(), 25)
        self.assertEqual(second.qsize(), 25)
        self.assertEqual(listener.get_nowait(), packet(0.1))
        self.assertEqual(self.clips(), [])
        self.assertFalse(self.hub.status()["recorder"]["enabled"])
        self.hub.unsubscribe(key)
        self.hub.unsubscribe(second_key)
        self.assertEqual(self.hub.status()["live_audio"]["listeners"], 0)

    def test_old_audio_is_dropped_instead_of_increasing_latency(self):
        _key, listener = self.hub.subscribe()
        for value in range(30):
            self.hub.feed_packet(packet(value / 100))
        self.assertEqual(listener.get_nowait(), packet(0.05))

    def test_listener_limit_and_disable_end_existing_streams(self):
        listeners = [self.hub.subscribe()[1] for _ in range(8)]
        with self.assertRaises(ValueError):
            self.hub.subscribe()
        self.hub.feed_packet(packet(0.1))
        self.hub.update_settings(dict(self.settings, input_enabled=False))
        for listener in listeners:
            self.assertIsNone(listener.get_nowait())
            self.assertTrue(listener.empty())
        self.assertFalse(self.hub.status()["live_audio"]["receiving"])

    def test_bad_datagrams_do_not_reach_listeners_or_crash_recording(self):
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        for data in (b"bad", packet(float("nan")), packet(float("inf")), b"x" * 65508):
            self.assertFalse(self.hub.feed_packet(data))
        self.assertTrue(listener.empty())
        self.assertEqual(self.clips(), [])
        self.assertIn("Ignored", self.hub.status()["live_audio"]["last_error"])
        self.assertTrue(self.hub.feed_packet(packet(0.1)))
        self.assertIsNone(self.hub.status()["live_audio"]["last_error"])

    def test_record_toggle_creates_playable_wav_with_preroll_and_finalizes(self):
        self.hub.set_recording(True)
        for _ in range(25):
            self.hub.feed_packet(packet(0.0))
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.hub.feed_packet(packet(0.25))
        self.assertTrue(self.hub.status()["recorder"]["active"])
        self.assertEqual(self.clips(), [])  # Active .part is not served as an archive.
        self.assertTrue(self.hub.status()["recorder"]["current_file"].endswith(".wav.part"))
        self.hub.set_recording(False)
        self.assertEqual(len(self.clips()), 1)
        self.assertEqual(list(self.root.glob("*.part")), [])
        with wave.open(str(self.clips()[0]), "rb") as clip:
            self.assertEqual((clip.getframerate(), clip.getnchannels(), clip.getsampwidth()), (48000, 1, 2))
            self.assertEqual(clip.getnframes(), 24000)
            samples = array("h", clip.readframes(24000))
            if sys.byteorder != "little":
                samples.byteswap()
            self.assertEqual(samples[0], 0)
            self.assertGreater(samples[-1], 8000)
        self.hub.feed_packet(packet(0.5))
        self.assertEqual(len(self.clips()), 1)

    def test_threshold_hang_and_packet_timeout_split_recordings(self):
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.0001))
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.hub.feed_packet(packet(0.1))
        self.advance(3.1)
        self.hub.feed_packet(packet(0.0))
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.hub.feed_packet(packet(0.2))
        self.advance(5.1)
        self.hub.tick()
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.assertEqual(len(self.clips()), 2)
        self.assertFalse(self.hub.status()["live_audio"]["receiving"])

    def test_source_flush_finalizes_but_keeps_live_subscription(self):
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.assertTrue(self.hub.feed_packet(b""))
        self.assertEqual(len(self.clips()), 1)
        self.assertEqual(self.hub.status()["live_audio"]["listeners"], 1)
        self.assertEqual(listener.get_nowait(), packet(0.2))
        with self.assertRaises(queue.Empty):
            listener.get_nowait()

    def test_clip_rotation_never_exceeds_frame_limit(self):
        self.hub.MAX_CLIP_SECONDS = 1
        self.hub.set_recording(True)
        for _ in range(130):
            self.hub.feed_packet(packet(0.2))
            self.advance(0.02)
        self.hub.set_recording(False)
        clips = self.clips()
        self.assertEqual(len(clips), 3)
        counts = []
        for path in clips:
            with wave.open(str(path), "rb") as clip:
                counts.append(clip.getnframes())
        self.assertTrue(all(count <= 48000 for count in counts))
        self.assertEqual(sum(counts), 130 * 960)

    def test_retention_prunes_only_closed_application_files(self):
        old = self.root / "rx_20200101_000000_aaaaaaaaaaaa.wav"
        old.write_bytes(b"old")
        os.utime(old, (self.wall - 8 * 86400, self.wall - 8 * 86400))
        oldest = self.root / "rx_20230101_000000_bbbbbbbbbbbb.wav"
        newest = self.root / "rx_20230101_000001_cccccccccccc.wav"
        # Sparse files exercise the 64 MiB minimum without allocating 80 MiB.
        with oldest.open("wb") as stream:
            stream.truncate(40 * 1024 * 1024)
        with newest.open("wb") as stream:
            stream.truncate(40 * 1024 * 1024)
        os.utime(oldest, (self.wall - 2, self.wall - 2))
        os.utime(newest, (self.wall - 1, self.wall - 1))
        unrelated = self.root / "personal.wav"
        unrelated.write_bytes(b"preserve")
        fake_prefix = self.root / "rx_personal.wav"
        fake_prefix.write_bytes(b"preserve")
        partial = self.root / "rx_20200101_000000_eeeeeeeeeeee.wav.part"
        partial.write_bytes(b"preserve")
        outside = self.root / "outside.txt"
        outside.write_bytes(b"preserve")
        link = self.root / "rx_20200101_000000_dddddddddddd.wav"
        link.symlink_to(outside)
        self.hub.update_settings(dict(self.settings, max_archive_mb=64))
        self.assertFalse(old.exists())
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        for path in (unrelated, fake_prefix, partial, outside, link):
            self.assertTrue(path.exists())

    def test_low_space_leaves_recording_requested_without_creating_archive(self):
        self.space.return_value.free = 100 * 1024 * 1024
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        value = self.hub.status()["recorder"]
        self.assertTrue(value["enabled"])
        self.assertFalse(value["active"])
        self.assertIn("free disk space", value["last_error"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.space.return_value.free = 1024 * 1024 * 1024
        self.advance(1.1)
        self.hub.feed_packet(packet(0.2))
        self.assertTrue(self.hub.status()["recorder"]["active"])

    def test_quiet_archives_still_age_out_without_new_audio_or_settings_changes(self):
        archived = self.root / "rx_20230101_000000_aaaaaaaaaaaa.wav"
        archived.write_bytes(b"closed clip")
        os.utime(archived, (self.wall - 6 * 86400, self.wall - 6 * 86400))
        unrelated = self.root / "personal.wav"
        unrelated.write_bytes(b"preserve")
        self.hub.tick()
        self.assertTrue(archived.exists())
        self.advance(2 * 86400)
        self.hub.tick()
        self.assertFalse(archived.exists())
        self.assertTrue(unrelated.exists())

    def test_low_space_during_capture_closes_the_file_and_keeps_live_listening(self):
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.space.return_value.free = 100 * 1024 * 1024
        self.advance(1.1)
        self.hub.feed_packet(packet(0.3))
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.assertEqual(len(self.clips()), 1)
        self.assertEqual(listener.qsize(), 2)
        with wave.open(str(self.clips()[0]), "rb") as clip:
            self.assertEqual(clip.getnframes(), 960)

    def test_keep_indefinitely_skips_age_expiry_but_keeps_oldest_first_size_rotation(self):
        old = self.closed_clip("a", size=3, age=365 * 86400)
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True, max_archive_mb=64))
        self.hub.tick()
        self.assertTrue(old.exists())
        self.assertTrue(self.hub.status()["recorder"]["keep_indefinitely"])
        oldest_large = self.closed_clip("b", size=40 * 1024 * 1024, age=20)
        newest = self.closed_clip("c", size=40 * 1024 * 1024, age=10)
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True, max_archive_mb=64))
        self.assertFalse(old.exists())
        self.assertFalse(oldest_large.exists())
        self.assertTrue(newest.exists())

    def test_disabling_keep_indefinitely_resumes_age_expiry_and_preserves_current_part(self):
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True))
        old = self.closed_clip("a", size=3, age=9 * 86400)
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        current = self.hub._part_path
        self.assertTrue(old.exists())
        self.hub.update_settings(dict(self.settings, keep_indefinitely=False))
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())
        self.assertTrue(self.hub.status()["recorder"]["active"])
        self.assertFalse(self.hub.status()["recorder"]["keep_indefinitely"])

    def test_low_free_space_rotates_oldest_closed_clip_and_preserves_current_unrelated_and_links(self):
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True))
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        current = self.hub._part_path
        oldest = self.closed_clip("a", age=100)
        newest = self.closed_clip("b", age=50)
        unrelated = self.root / "personal.wav"
        unrelated.write_bytes(b"keep")
        partial = self.root / "rx_20230101_000000_cccccccccccc.wav.part"
        partial.write_bytes(b"keep")
        link = self.root / "rx_20230101_000000_dddddddddddd.wav"
        link.symlink_to(unrelated)
        def available(_path):
            freed = (0 if oldest.exists() else 32 * 1024 * 1024)
            return mock.Mock(free=240 * 1024 * 1024 + freed)
        self.space.side_effect = available
        self.advance(1.1)
        self.hub.feed_packet(packet(0.3))
        self.assertFalse(oldest.exists())
        for path in (newest, current, unrelated, partial, link):
            self.assertTrue(path.exists(), path)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.hub._part_path, current)
        self.assertTrue(self.hub.status()["recorder"]["active"])
        self.assertEqual(listener.qsize(), 2)

    def test_above_free_space_floor_does_not_rotate_archives_for_extra_headroom(self):
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True))
        closed = self.closed_clip("a")
        self.space.return_value.free = self.hub.FREE_DISK_FLOOR + 1
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.assertTrue(closed.exists())
        self.assertTrue(self.hub.status()["recorder"]["active"])

    def test_unlink_that_does_not_free_blocks_pauses_with_bounded_retry_and_live_continues(self):
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True))
        closed = self.closed_clip("a", size=3)
        self.space.return_value.free = 100 * 1024 * 1024
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.assertFalse(closed.exists())
        self.assertFalse(self.hub.status()["recorder"]["active"])
        checks = self.space.call_count
        for _ in range(10):
            self.hub.feed_packet(packet(0.3))
        self.assertEqual(self.space.call_count, checks)
        self.assertEqual(listener.qsize(), 11)
        self.assertIn("free disk space", self.hub.status()["recorder"]["last_error"])
        self.space.return_value.free = 1024 * 1024 * 1024
        self.advance(1.1)
        self.hub.feed_packet(packet(0.2))
        self.assertTrue(self.hub.status()["recorder"]["active"])

    def test_unlink_failures_attempt_each_candidate_once_and_do_not_retry_each_packet(self):
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True))
        oldest = self.closed_clip("a", age=100)
        newest = self.closed_clip("b", age=50)
        self.space.return_value.free = 100 * 1024 * 1024
        self.hub.set_recording(True)
        attempted = []
        original_unlink = Path.unlink
        def unavailable(path, *args, **kwargs):
            if path in (oldest, newest):
                attempted.append(path)
                raise PermissionError("read-only archives")
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", new=unavailable):
            self.hub.feed_packet(packet(0.2))
            self.hub.feed_packet(packet(0.3))
        self.assertEqual(attempted, [oldest, newest])
        self.assertTrue(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertFalse(self.hub.status()["recorder"]["active"])

    def test_space_check_failure_after_one_deletion_stops_further_reclamation(self):
        self.hub.update_settings(dict(self.settings, keep_indefinitely=True))
        oldest = self.closed_clip("a", age=100)
        newest = self.closed_clip("b", age=50)
        self.space.side_effect = [mock.Mock(free=100 * 1024 * 1024), OSError("disk check failed")]
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.assertEqual(listener.qsize(), 1)
        self.assertIn("Cannot check archive space", self.hub.status()["recorder"]["last_error"])

    def test_unwritable_archive_retries_at_most_once_per_second_and_keeps_live_audio(self):
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        original_open = Path.open
        attempts = []
        def unavailable(path, *args, **kwargs):
            if path.name.endswith(".wav.part"):
                attempts.append(path)
                raise PermissionError("archive is read-only")
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", new=unavailable):
            self.hub.feed_packet(packet(0.2))
            self.hub.feed_packet(packet(0.3))
            self.assertEqual(len(attempts), 1)
            self.advance(1.1)
            self.hub.feed_packet(packet(0.4))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(listener.qsize(), 3)
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.assertIn("Cannot start RX recording", self.hub.status()["recorder"]["last_error"])

    def test_write_failure_finalizes_and_backs_off_without_interrupting_live_audio(self):
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        with mock.patch.object(self.hub._wave, "writeframesraw", side_effect=OSError("write failed")):
            self.hub.feed_packet(packet(0.3))
        self.assertFalse(self.hub.status()["recorder"]["active"])
        checks = self.space.call_count
        self.hub.feed_packet(packet(0.4))
        self.assertEqual(self.space.call_count, checks)
        self.assertEqual(listener.qsize(), 3)
        self.advance(1.1)
        self.hub.feed_packet(packet(0.5))
        self.assertTrue(self.hub.status()["recorder"]["active"])

    def test_disabling_the_source_finalizes_a_clip_without_disarming_the_recorder(self):
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.hub.update_settings(dict(self.settings, input_enabled=False))
        self.assertEqual(len(self.clips()), 1)
        state = self.hub.status()
        self.assertFalse(state["live_audio"]["configured"])
        self.assertTrue(state["recorder"]["enabled"])
        self.assertFalse(state["recorder"]["active"])
        self.assertFalse(self.hub.feed_packet(packet(0.2)))

    def test_startup_preference_changes_do_not_toggle_the_running_recorder(self):
        self.hub.update_settings(dict(self.settings, record_on_startup=True))
        self.assertFalse(self.hub.status()["recorder"]["enabled"])
        self.hub.set_recording(True)
        self.hub.update_settings(dict(self.settings, record_on_startup=False))
        self.assertTrue(self.hub.status()["recorder"]["enabled"])

    def test_bind_failure_is_reported_and_close_does_not_leak_thread(self):
        with mock.patch.object(module.socket, "socket") as factory:
            factory.return_value.bind.side_effect = OSError("Address already in use")
            self.hub.start()
            self.assertIn("Cannot receive", self.hub.status()["live_audio"]["last_error"])
            self.hub.close()
            self.assertFalse(self.hub._thread.is_alive())
            factory.return_value.close.assert_called_once()

    def test_disable_and_close_finalize_wav_and_unblock_subscribers(self):
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.hub.close()
        self.assertIsNone(listener.get_nowait())
        self.assertEqual(len(self.clips()), 1)
        self.assertFalse(self.hub.feed_packet(packet(0.2)))
        self.hub.close()  # Idempotent cleanup.


if __name__ == "__main__":
    unittest.main()
