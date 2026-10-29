"""Deterministic RX audio/recording checks; no radio or ALSA device is opened."""
from array import array
import importlib.util
import os
from pathlib import Path
import queue
import socket
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


def tx_packet(value, frames=960):
    samples = array("h", [value] * frames)
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
            sockets = [mock.Mock(), mock.Mock()]
            for sock in sockets:
                sock.bind.side_effect = OSError("Address already in use")
            factory.side_effect = sockets
            self.hub.start()
            self.assertIn("Cannot receive", self.hub.status()["live_audio"]["last_error"])
            self.hub.close()
            self.assertFalse(self.hub._thread.is_alive())
            for sock in sockets:
                sock.close.assert_called_once()

    def use_live_probe(self, live=True):
        state = {"live": live}
        probe = mock.Mock(side_effect=lambda: state["live"])
        self.hub._live_mode_probe = probe
        self.hub._mode_next_at = float("-inf")
        return state, probe

    def test_transmit_mix_replaces_raw_rx_and_generated_audio_reaches_archive_and_listener(self):
        self.use_live_probe()
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.assertTrue(self.hub.feed_packet(packet(0.75)))
        self.assertTrue(listener.empty())
        generated = tx_packet(8192)
        self.assertTrue(self.hub.feed_tx_packet(generated))
        self.assertEqual(listener.get_nowait(), packet(0.25))
        self.assertTrue(listener.empty())  # Repeated RX was not added a second time.
        state = self.hub.status()["live_audio"]
        self.assertEqual(state["source"], "tx_mix")
        self.assertTrue(state["includes_outgoing"])
        self.assertTrue(state["sources"]["raw_rx"]["receiving"])
        self.hub.set_recording(False)
        with wave.open(str(self.clips()[0]), "rb") as clip:
            self.assertEqual(clip.getnframes(), 960)
            self.assertEqual(clip.readframes(960), generated)

    def test_receive_only_mode_ignores_transmit_packets_and_transmit_setting_selects_raw_rx(self):
        state, _probe = self.use_live_probe(live=False)
        _key, listener = self.hub.subscribe()
        self.hub.feed_tx_packet(tx_packet(8192))
        self.assertTrue(listener.empty())
        self.hub.feed_packet(packet(0.5))
        self.assertEqual(listener.get_nowait(), packet(0.5))
        state["live"] = True
        self.hub.update_settings(dict(self.settings, monitor_transmit_audio=False))
        self.hub.feed_tx_packet(tx_packet(8192))
        self.assertTrue(listener.empty())
        self.hub.feed_packet(packet(0.125))
        self.assertEqual(listener.get_nowait(), packet(0.125))
        self.assertEqual(self.hub.status()["live_audio"]["source"], "raw_rx")
        self.assertFalse(self.hub.status()["live_audio"]["includes_outgoing"])

    def test_transmit_conversion_handles_full_signed_range_and_preserves_archive_pcm(self):
        self.use_live_probe()
        _key, listener = self.hub.subscribe()
        samples = array("h", [-32768, -16384, 0, 16384, 32767])
        if sys.byteorder != "little":
            samples.byteswap()
        raw = samples.tobytes()
        self.hub.set_recording(True)
        self.hub.feed_tx_packet(raw)
        floats = array("f")
        floats.frombytes(listener.get_nowait())
        if sys.byteorder != "little":
            floats.byteswap()
        self.assertEqual(list(floats), [-1.0, -0.5, 0.0, 0.5, 32767 / 32768])
        self.hub.set_recording(False)
        with wave.open(str(self.clips()[0]), "rb") as clip:
            self.assertEqual(clip.readframes(5), raw)

    def test_malformed_transmit_packets_do_not_flush_recording_or_poison_raw_source_error(self):
        self.use_live_probe()
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_tx_packet(tx_packet(8192))
        listener.get_nowait()
        for data in (b"", b"x", b"x" * 65506):
            self.assertFalse(self.hub.feed_tx_packet(data))
        self.assertTrue(listener.empty())
        self.assertTrue(self.hub.status()["recorder"]["active"])
        self.assertIn("Ignored", self.hub.status()["live_audio"]["last_error"])
        self.assertIsNone(self.hub.status()["live_audio"]["sources"]["raw_rx"]["last_error"])
        self.hub.feed_tx_packet(tx_packet(8192))
        self.assertIsNone(self.hub.status()["live_audio"]["last_error"])
        self.hub.feed_packet(packet(float("nan")))
        self.assertIsNone(self.hub.status()["live_audio"]["last_error"])

    def test_transmit_packet_gap_finalizes_after_hang_and_drops_stale_preroll(self):
        self.use_live_probe()
        self.hub.set_recording(True)
        self.hub.feed_tx_packet(tx_packet(8192))
        self.advance(self.hub.HANG_SECONDS + 0.1)
        self.hub.tick()
        self.assertEqual(len(self.clips()), 1)
        self.assertFalse(self.hub.status()["recorder"]["active"])
        self.hub.feed_tx_packet(tx_packet(1))
        self.assertGreater(self.hub._preroll_frames, 0)
        self.advance(self.hub.PREROLL_GAP_SECONDS + 0.1)
        self.hub.tick()
        self.assertEqual(self.hub._preroll_frames, 0)
        self.hub.feed_tx_packet(tx_packet(4096))
        self.hub.set_recording(False)
        counts = []
        for path in self.clips():
            with wave.open(str(path), "rb") as clip:
                counts.append(clip.getnframes())
        self.assertEqual(counts, [960, 960])

    def test_source_switch_finalizes_clip_clears_listener_backlog_and_caches_probe(self):
        state, probe = self.use_live_probe(live=False)
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.5))
        self.hub.status()
        self.hub.tick()
        self.assertEqual(probe.call_count, 1)
        state["live"] = True
        self.advance(0.21)
        self.hub.feed_tx_packet(tx_packet(8192))
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(self.clips()), 1)
        self.assertEqual(listener.qsize(), 1)
        self.assertEqual(listener.get_nowait(), packet(0.25))
        self.hub.update_settings(dict(self.settings, monitor_transmit_audio=False))
        self.assertEqual(len(self.clips()), 2)
        self.assertEqual(self.hub._preroll_frames, 0)
        self.assertFalse(self.hub.status()["live_audio"]["receiving"])

    def test_recording_off_does_not_interrupt_transmit_listeners(self):
        self.use_live_probe()
        _key, listener = self.hub.subscribe()
        self.hub.feed_tx_packet(tx_packet(8192))
        self.assertEqual(listener.get_nowait(), packet(0.25))
        self.assertFalse(self.hub.status()["recorder"]["enabled"])
        self.assertEqual(self.clips(), [])
        self.hub.set_recording(True)
        self.hub.feed_tx_packet(tx_packet(4096))
        self.hub.set_recording(False)
        self.hub.feed_tx_packet(tx_packet(8192))
        self.assertEqual(listener.qsize(), 2)
        self.assertEqual(len(self.clips()), 1)

    def test_transmit_bind_failure_is_reported_without_falling_back_to_raw_rx(self):
        self.use_live_probe()
        rx, tx = mock.Mock(), mock.Mock()
        rx.recvfrom.side_effect = BlockingIOError()
        tx.bind.side_effect = OSError("Address already in use")
        with mock.patch.object(module.socket, "socket", side_effect=(rx, tx)):
            self.hub._bind_locked()
        state = self.hub.status()["live_audio"]
        self.assertEqual(state["source"], "tx_mix")
        self.assertIn("tx_mix", state["last_error"])
        self.assertFalse(state["includes_outgoing"])
        self.assertIsNone(state["sources"]["raw_rx"]["last_error"])
        _key, listener = self.hub.subscribe()
        self.hub.feed_packet(packet(0.5))
        self.assertTrue(listener.empty())
        rx.close.assert_not_called()

    def test_source_change_discards_oversized_udp_backlog_with_bounded_work(self):
        state, _probe = self.use_live_probe(live=False)
        self.hub.status()
        stale = mock.Mock()
        stale.recvfrom.return_value = (b"old", ("127.0.0.1", 1))
        self.hub._sockets["tx_mix"] = stale
        state["live"] = True
        self.advance(0.21)
        self.hub.status()
        self.assertEqual(stale.recvfrom.call_count, self.hub.DRAIN_BATCH)
        stale.close.assert_called_once()
        self.assertIsNone(self.hub._sockets["tx_mix"])

    def test_dual_loopback_sockets_stream_only_the_selected_source_and_close_cleanly(self):
        self.use_live_probe()
        self.hub.PORT = self.hub.TX_PORT = 0
        _key, listener = self.hub.subscribe()
        self.hub.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(packet(0.75), self.hub._socket.getsockname())
                sender.sendto(tx_packet(8192), self.hub._sockets["tx_mix"].getsockname())
            self.assertEqual(listener.get(timeout=2), packet(0.25))
            self.assertTrue(listener.empty())
        finally:
            self.hub.close()
        self.assertFalse(self.hub._thread.is_alive())
        self.assertTrue(all(sock is None for sock in self.hub._sockets.values()))

    def test_disable_and_close_finalize_wav_and_unblock_subscribers(self):
        _key, listener = self.hub.subscribe()
        self.hub.set_recording(True)
        self.hub.feed_packet(packet(0.2))
        self.hub.close()
        self.assertIsNone(listener.get_nowait())
        self.assertEqual(len(self.clips()), 1)
        self.assertFalse(self.hub.feed_packet(packet(0.2)))
        self.hub.close()  # Idempotent cleanup.


    def test_private_raw_consumers_get_rx_while_listeners_select_tx_mix(self):
        self.hub._live_mode_probe = lambda: True
        key, raw = self.hub.subscribe_raw()
        _, listener = self.hub.subscribe()
        self.hub.feed_tx_packet(tx_packet(8192))
        self.hub.feed_packet(packet(0.75))
        events = []
        while not raw.empty():
            events.append(raw.get_nowait())
        self.assertEqual([value for kind, value, _ in events if kind == 'audio'], [packet(0.75)])
        self.assertEqual(listener.get_nowait(), packet(0.25))
        self.assertTrue(listener.empty())
        self.hub.unsubscribe_raw(key)

    def test_raw_consumer_overflow_resets_only_its_bounded_queue(self):
        _, raw = self.hub.subscribe_raw()
        _, listener = self.hub.subscribe()
        for number in range(self.hub.RAW_QUEUE_CHUNKS + 8):
            self.hub.feed_packet(packet(number / 100))
        self.assertLessEqual(raw.qsize(), self.hub.RAW_QUEUE_CHUNKS)
        events = []
        while not raw.empty():
            events.append(raw.get_nowait())
        self.assertEqual(events[0][0], 'reset')
        self.assertEqual(events[-1][1], packet((self.hub.RAW_QUEUE_CHUNKS + 7) / 100))
        self.assertEqual(listener.qsize(), self.hub.QUEUE_CHUNKS)
        self.assertEqual(self.clips(), [])

    def test_raw_gaps_and_flush_markers_preserve_preceding_audio_then_reset(self):
        _, raw = self.hub.subscribe_raw()
        raw.get_nowait()
        self.hub.feed_packet(packet(0.1))
        self.advance(1)
        self.hub.feed_packet(packet(0.2))
        self.hub.feed_packet(b'')
        events = []
        while not raw.empty():
            events.append(raw.get_nowait())
        self.assertEqual([event[0] for event in events], ['audio', 'flush', 'audio', 'flush'])
        self.assertEqual(events[0][1], packet(0.1))
        self.assertEqual(events[2][1], packet(0.2))

    def test_raw_tap_validates_samples_and_source_off_and_close_discard_backlogs(self):
        _, raw = self.hub.subscribe_raw()
        raw.get_nowait()
        for value in (b'bad', packet(float('nan')), packet(float('inf'))):
            self.assertFalse(self.hub.feed_packet(value))
        self.assertEqual(raw.get_nowait()[0], 'reset')
        self.assertTrue(raw.empty())
        self.hub.feed_packet(packet(0.1))
        self.hub.update_settings(dict(self.settings, input_enabled=False))
        self.assertEqual(raw.get_nowait()[:2], ('state', False))
        self.assertTrue(raw.empty())
        self.assertFalse(self.hub.raw_input_enabled())
        self.hub.close()
        self.assertEqual(raw.get_nowait()[0], 'closed')
        self.assertTrue(raw.empty())

    def test_raw_consumer_limit_and_unsubscribe_are_separate_from_browser_listeners(self):
        first, raw = self.hub.subscribe_raw()
        self.hub.subscribe_raw()
        with self.assertRaises(ValueError):
            self.hub.subscribe_raw()
        for _ in range(self.hub.MAX_LISTENERS):
            self.hub.subscribe()
        self.hub.unsubscribe_raw(first)
        self.assertEqual(raw.get_nowait()[0], 'closed')
        self.hub.subscribe_raw()
        self.assertEqual(self.hub.status()['live_audio']['listeners'], self.hub.MAX_LISTENERS)

    def test_delete_one_reviewed_closed_recording_without_changing_recording_or_feed(self):
        selected = self.root / 'selected recording.wav'
        selected.write_bytes(b'closed audio')
        other = self.root / 'other.wav'; other.write_bytes(b'keep')
        self.hub.set_recording(True)
        before = self.hub.status()
        revision = self.hub.recording_revision(selected.name, selected.stat())
        self.hub.delete_recording(selected.name, revision)
        self.assertFalse(selected.exists()); self.assertEqual(other.read_bytes(), b'keep')
        after = self.hub.status()
        self.assertEqual(after['recorder']['enabled'], before['recorder']['enabled'])
        self.assertEqual(after['live_audio']['configured'], before['live_audio']['configured'])

    def test_delete_recording_refuses_stale_replaced_missing_and_invalid_targets(self):
        selected = self.root / 'selected.wav'; selected.write_bytes(b'old')
        revision = self.hub.recording_revision(selected.name, selected.stat())
        selected.unlink(); selected.write_bytes(b'replacement')
        with self.assertRaises(ValueError):self.hub.delete_recording(selected.name, revision)
        self.assertEqual(selected.read_bytes(), b'replacement')
        for name in ('../selected.wav', '/tmp/selected.wav', 'a\\b.wav', '.hidden.wav',
                     'selected.wav.part', 'settings.json', 'bad\0.wav', 'x'*256+'.wav'):
            with self.subTest(name=name), self.assertRaises(ValueError):self.hub.delete_recording(name, revision)
        with self.assertRaises(ValueError):self.hub.delete_recording(selected.name, 'not-a-revision')
        with self.assertRaises(FileNotFoundError):self.hub.delete_recording('missing.wav', revision)
        self.assertTrue(selected.exists())

    def test_delete_recording_never_removes_current_recording_or_parts(self):
        self.hub.set_recording(True); self.hub.feed_packet(packet(0.5))
        part, final = self.hub._part_path, self.hub._final_path
        self.assertIsNotNone(part)
        revision = self.hub.recording_revision(part.name, part.stat())
        with self.assertRaises(ValueError):self.hub.delete_recording(part.name, revision)
        final.write_bytes(b'active final path is reserved')
        revision = self.hub.recording_revision(final.name, final.stat())
        with self.assertRaises(ValueError):self.hub.delete_recording(final.name, revision)
        self.assertTrue(part.exists()); self.assertTrue(self.hub.status()['recorder']['active'])
        final.unlink()

    def test_delete_recording_rejects_symlinks_hardlinks_and_directory_symlinks(self):
        outside = self.root.parent / (self.root.name+'-outside.wav')
        outside.write_bytes(b'outside must remain')
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.root / 'linked.wav'; link.symlink_to(outside)
        with self.assertRaises(OSError):self.hub.delete_recording(link.name, 'a'*64)
        link.unlink(); os.link(outside, link)
        revision = self.hub.recording_revision(link.name, link.stat())
        with self.assertRaises(ValueError):self.hub.delete_recording(link.name, revision)
        self.assertEqual(outside.read_bytes(), b'outside must remain')
        parent_link = self.root / 'directory-link'; parent_link.symlink_to(self.root, target_is_directory=True)
        other = AudioHub(parent_link, self.settings)
        try:
            with self.assertRaises(OSError):other.delete_recording(link.name, revision)
        finally:other.close()

    def test_delete_recording_detects_a_change_after_open_and_preserves_failed_unlinks(self):
        selected=self.root/'selected.wav';selected.write_bytes(b'old')
        revision=self.hub.recording_revision(selected.name, selected.stat())
        real_stat=module.os.stat
        def changed_stat(*args, **kwargs):
            if args[0] == selected.name and kwargs.get('dir_fd') is not None:
                selected.write_bytes(b'changed during confirmation')
            return real_stat(*args, **kwargs)
        with mock.patch.object(module.os, 'stat', side_effect=changed_stat):
            with self.assertRaises(ValueError):self.hub.delete_recording(selected.name, revision)
        self.assertEqual(selected.read_bytes(), b'changed during confirmation')
        revision=self.hub.recording_revision(selected.name, selected.stat())
        with mock.patch.object(module.os, 'unlink', side_effect=OSError('disk fault')):
            with self.assertRaises(OSError):self.hub.delete_recording(selected.name, revision)
        self.assertTrue(selected.exists())

    def test_recording_delete_syncs_the_directory_after_unlink(self):
        selected=self.root/'selected.wav';selected.write_bytes(b'closed')
        revision=self.hub.recording_revision(selected.name,selected.stat())
        events=[];real_unlink=module.os.unlink
        def unlink(*args,**kwargs):
            events.append('unlink');return real_unlink(*args,**kwargs)
        with mock.patch.object(module.os,'unlink',side_effect=unlink), mock.patch.object(module.os,'fsync',side_effect=lambda fd:events.append('sync')):
            self.hub.delete_recording(selected.name,revision)
        self.assertEqual(events,['unlink','sync']);self.assertFalse(selected.exists())

    def selected_recordings(self, *names):
        values = []
        for name in names:
            path = self.root / name
            path.write_bytes(b'saved audio')
            values.append({'id': name, 'revision': self.hub.recording_revision(name, path.stat())})
        return values

    def test_bulk_recordings_delete_only_reviewed_files_and_preserve_active_audio(self):
        selection = self.selected_recordings('first.wav', 'second.ogg', 'keep.wav')
        self.hub.set_recording(True); self.hub.feed_packet(packet(0.5))
        part = self.hub._part_path
        before = self.hub.status(); settings = dict(self.hub._settings)
        _, listener = self.hub.subscribe()
        events = []; real_unlink = module.os.unlink
        def unlink(name, **kwargs):
            events.append(name); return real_unlink(name, **kwargs)
        with mock.patch.object(module.os, 'unlink', side_effect=unlink), mock.patch.object(module.os, 'fsync', side_effect=lambda fd: events.append('sync')):
            result = self.hub.delete_recordings(selection[:2])
        self.assertEqual(result, {'deleted': ['first.wav', 'second.ogg'], 'failed': []})
        self.assertEqual(events, ['first.wav', 'second.ogg', 'sync'])
        self.assertFalse((self.root/'first.wav').exists()); self.assertTrue((self.root/'keep.wav').exists())
        self.assertTrue(part.exists()); self.assertTrue(self.hub.status()['recorder']['active'])
        self.assertEqual(self.hub._settings, settings)
        self.assertEqual(self.hub.status()['recorder']['enabled'], before['recorder']['enabled'])
        self.hub.feed_packet(packet(0.2)); self.assertEqual(listener.get_nowait(), packet(0.2))

    def test_bulk_recording_preflight_checks_entire_selection_before_any_unlink(self):
        first, second = self.selected_recordings('first.wav', 'second.wav')
        self.hub.set_recording(True); self.hub.feed_packet(packet(0.5))
        current = self.hub._final_path; current.write_bytes(b'reserved')
        active = {'id': current.name, 'revision': self.hub.recording_revision(current.name, current.stat())}
        linked = self.root/'linked.wav'; linked.symlink_to(self.root/'second.wav')
        cases = [(dict(second, revision='0'*64), ValueError),
                 ({'id': 'missing.wav', 'revision': second['revision']}, FileNotFoundError),
                 (active, ValueError), ({'id': linked.name, 'revision': second['revision']}, OSError)]
        try:
            for last, error in cases:
                with self.subTest(last=last['id']), mock.patch.object(module.os, 'unlink') as unlink:
                    with self.assertRaises(error): self.hub.delete_recordings([first, last])
                    unlink.assert_not_called()
                self.assertTrue((self.root/'first.wav').exists())
                self.assertTrue((self.root/'second.wav').exists())
        finally:
            current.unlink()

    def test_bulk_recording_request_shape_bounds_and_duplicates_fail_without_actions(self):
        selected = self.selected_recordings('first.wav')[0]
        cases = [None, [], (), [selected]*101, [selected, selected],
                 [dict(selected, extra=True)], [{'id': selected['id']}], [None],
                 [dict(selected, id=1)], [dict(selected, revision='bad')],
                 [dict(selected, id='../first.wav')]]
        for selection in cases:
            with self.subTest(selection=selection), mock.patch.object(module.os, 'unlink') as unlink:
                with self.assertRaises(ValueError): self.hub.delete_recordings(selection)
                unlink.assert_not_called()
        self.assertTrue((self.root/'first.wav').exists())

    def test_bulk_recording_limit_accepts_exactly_one_hundred_explicit_files(self):
        selected = self.selected_recordings(*(f'clip-{number}.wav' for number in range(100)))
        result = self.hub.delete_recordings(selected)
        self.assertEqual(result, {'deleted': [value['id'] for value in selected], 'failed': []})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_bulk_recording_partial_io_failure_reports_removed_and_unattempted_files(self):
        selected = self.selected_recordings('first.wav', 'second.wav', 'third.wav')
        real_unlink = module.os.unlink
        def unlink(name, **kwargs):
            if name == 'second.wav': raise OSError('synthetic disk fault')
            return real_unlink(name, **kwargs)
        with mock.patch.object(module.os, 'unlink', side_effect=unlink), mock.patch.object(module.os, 'fsync') as sync:
            result = self.hub.delete_recordings(selected)
        self.assertEqual(result, {'deleted': ['first.wav'], 'failed': [
            {'id': 'second.wav', 'reason': 'storage_error'}, {'id': 'third.wav', 'reason': 'not_attempted'}]})
        sync.assert_called_once()
        self.assertFalse((self.root/'first.wav').exists())
        self.assertTrue((self.root/'second.wav').exists()); self.assertTrue((self.root/'third.wav').exists())

    def test_bulk_recordings_revalidate_each_file_after_preflight_and_stop_on_race(self):
        selected = self.selected_recordings('first.wav', 'second.wav', 'third.wav')
        remove = self.hub._delete_recording_locked
        def changed(name, revision, directory):
            if name == 'second.wav': (self.root/name).write_bytes(b'manually replaced after preflight')
            return remove(name, revision, directory)
        with mock.patch.object(self.hub, '_delete_recording_locked', side_effect=changed):
            result = self.hub.delete_recordings(selected)
        self.assertEqual(result['deleted'], ['first.wav'])
        self.assertEqual(result['failed'], [{'id': 'second.wav', 'reason': 'changed'},
                                           {'id': 'third.wav', 'reason': 'not_attempted'}])
        self.assertEqual((self.root/'second.wav').read_bytes(), b'manually replaced after preflight')
        self.assertTrue((self.root/'third.wav').exists())

    def test_bulk_recording_sync_failure_truthfully_reports_already_removed_files(self):
        selected = self.selected_recordings('first.wav', 'second.wav')
        with mock.patch.object(module.os, 'fsync', side_effect=OSError('synthetic sync failure')):
            result = self.hub.delete_recordings(selected)
        self.assertEqual(result, {'deleted': ['first.wav', 'second.wav'], 'failed': [
            {'id': 'first.wav', 'reason': 'durability_unconfirmed'},
            {'id': 'second.wav', 'reason': 'durability_unconfirmed'}]})
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
