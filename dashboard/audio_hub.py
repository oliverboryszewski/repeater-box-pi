"""Local SvxLink audio taps for Wi-Fi listening and optional audio archives.

SvxLink 24.02 LocalRx RAW_AUDIO_UDP_DEST is a headerless stream of native float32
samples at CARD_SAMPLE_RATE, before its software squelch and DTMF muting.
On a Raspberry Pi that is float32 little-endian.  The supplied live template
uses 48 kHz mono.  This module never opens ALSA, serial ports, or PTT.

The optional native transmit monitor is 48 kHz mono signed 16-bit little-endian
PCM on a separate loopback port. In live mode it replaces, rather than sums
with, raw RX audio because the native transmit mix already includes repeated
speech. This is generated audio monitoring, not an off-air RF measurement.
"""
from __future__ import annotations

from array import array
from collections import deque
import datetime as dt
import math
from pathlib import Path
import queue
import re
import select
import shutil
import socket
import stat
import sys
import threading
import time
import uuid
import wave


class AudioHub:
    HOST = "127.0.0.1"
    PORT = 7355
    TX_PORT = 7356
    SAMPLE_RATE = 48000
    CHANNELS = 1
    STREAM_FORMAT = "f32le"
    STREAM_URL = "/api/live.pcm"
    STREAM_CHUNK_FRAMES = 960  # At most 20 ms in each subscriber queue item.
    QUEUE_CHUNKS = 25  # Drop old audio rather than accumulating listener delay.
    MAX_LISTENERS = 8
    PREROLL_FRAMES = SAMPLE_RATE // 2
    HANG_SECONDS = 3.0
    NO_PACKET_SECONDS = 5.0
    PREROLL_GAP_SECONDS = 0.5
    MODE_CACHE_SECONDS = 0.2
    RECEIVE_BATCH = 8
    DRAIN_BATCH = 64
    MAX_CLIP_SECONDS = 15 * 60
    FREE_DISK_FLOOR = 256 * 1024 * 1024
    FREE_DISK_BUFFER = 16 * 1024 * 1024
    SPACE_RETRY_SECONDS = 1.0
    ARCHIVE_NAME = re.compile(r"^rx_\d{8}_\d{6}_[0-9a-f]{12}\.wav$")

    def __init__(self, recordings_path, settings, *, monotonic=None,
                 wall_time=None, disk_usage=None, live_mode_probe=None):
        self.recordings_path = Path(recordings_path)
        self._clock = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self._disk_usage = disk_usage or shutil.disk_usage
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        # Retain the RX socket alias for existing local integrations.
        self._socket = None
        self._sockets = {"raw_rx": None, "tx_mix": None}
        self._started = False
        self._closed = False
        self._settings = self._clean_settings(settings)
        self._recording = self._settings["record_on_startup"]
        self._listeners = {}
        self._last_packet_mono = None
        self._last_packet_at = None
        self._input_error = None
        self._source_errors = {name: None for name in self._sockets}
        self._bind_errors = {name: None for name in self._sockets}
        self._source_last_mono = {name: None for name in self._sockets}
        self._source_last_at = {name: None for name in self._sockets}
        self._source = "raw_rx"
        self._live_mode_probe = live_mode_probe
        self._mode_next_at = float("-inf")
        self._mode_error = None
        self._recorder_error = None
        self._retry_at = {name: 0.0 for name in self._sockets}
        self._preroll = deque()
        self._preroll_frames = 0
        self._wave = None
        self._part_path = None
        self._final_path = None
        self._clip_frames = 0
        self._clip_started = None
        self._last_voice = None
        self._space_check_at = 0.0
        self._space_retry_at = 0.0
        self._next_retention_check = 0.0

    @staticmethod
    def _clean_settings(settings):
        def number(name, default, low, high):
            try:
                value = float(settings.get(name, default))
                if not math.isfinite(value):
                    return default
                return max(low, min(high, value))
            except (TypeError, ValueError):
                return default

        return {"input_enabled": bool(settings.get("input_enabled", False)),
                "monitor_transmit_audio": settings.get("monitor_transmit_audio", True) is True,
                "record_on_startup": bool(settings.get("record_on_startup", False)),
                "keep_indefinitely": settings.get("keep_indefinitely", False) is True,
                "retention_days": int(number("retention_days", 7, 1, 90)),
                "max_archive_mb": int(number("max_archive_mb", 4096, 64, 16384)),
                "recording_threshold_dbfs": number("recording_threshold_dbfs", -45, -70, -10)}

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Audio hub has been closed")
            if self._started:
                return
            self._started = True
            if self._settings["input_enabled"]:
                self._bind_locked()
            self._thread = threading.Thread(target=self._run, name="repeater-audio", daemon=True)
            self._thread.start()

    def _bind_locked(self):
        if self._closed or not self._settings["input_enabled"]:
            return
        for source, port in (("raw_rx", self.PORT), ("tx_mix", self.TX_PORT)):
            if self._sockets[source] is not None or self._clock() < self._retry_at[source]:
                continue
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # No SO_REUSEADDR: another hub must not silently take an input.
                sock.bind((self.HOST, port))
                sock.setblocking(False)
            except OSError as exc:
                if sock is not None:
                    sock.close()
                self._bind_errors[source] = f"Cannot receive local {source} audio on {self.HOST}:{port}: {exc}"
                self._retry_at[source] = self._clock() + 5
                continue
            self._sockets[source] = sock
            if source == "raw_rx":
                self._socket = sock
            self._bind_errors[source] = None

    def _clear_listener_queues_locked(self):
        for packets in self._listeners.values():
            while True:
                try:
                    packets.get_nowait()
                except queue.Empty:
                    break

    def _drain_inputs_locked(self):
        """Drop bounded stale UDP backlogs when changing the selected source."""
        for source, sock in tuple(self._sockets.items()):
            if sock is None:
                continue
            for _ in range(self.DRAIN_BATCH):
                try:
                    sock.recvfrom(65535)
                except (BlockingIOError, OSError):
                    break
            else:
                # A full bounded batch may still leave old datagrams. Replace
                # only that socket so an arbitrarily large backlog cannot leak
                # into the newly selected stream or archive.
                sock.close()
                self._sockets[source] = None
                self._retry_at[source] = 0.0
                if source == "raw_rx":
                    self._socket = None
        if self._started:
            self._bind_locked()

    def _refresh_source_locked(self, force=False):
        now = self._clock()
        if not force and now < self._mode_next_at:
            return
        self._mode_next_at = now + self.MODE_CACHE_SECONDS
        try:
            live = self._live_mode_probe is not None and self._live_mode_probe() is True
            self._mode_error = None
        except Exception:
            live = False
            self._mode_error = "Cannot determine whether the native transmit monitor is active"
        source = "tx_mix" if live and self._settings["monitor_transmit_audio"] else "raw_rx"
        if source != self._source:
            self._finish_clip_locked()
            self._clear_preroll_locked()
            self._clear_listener_queues_locked()
            self._drain_inputs_locked()
            self._source = source
            self._last_packet_mono = None
            self._last_packet_at = None
            self._last_voice = None

    def _source_error_locked(self, source):
        return self._bind_errors[source] or self._source_errors[source] or self._mode_error

    def _receive_failed_locked(self, source, sock, error):
        if sock is not self._sockets[source] or self._closed:
            return
        sock.close()
        self._sockets[source] = None
        if source == "raw_rx":
            self._socket = None
        self._bind_errors[source] = f"Local {source} audio receive failed: {error}"
        self._retry_at[source] = self._clock() + 5

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                self._bind_locked()
                self._refresh_source_locked()
                sockets = {sock: source for source, sock in self._sockets.items() if sock is not None}
            if not sockets:
                self._stop.wait(0.2)
            else:
                try:
                    ready, _, _ = select.select(list(sockets), [], [], 0.2)
                except (OSError, ValueError) as exc:
                    with self._lock:
                        current = {sock for sock in self._sockets.values() if sock is not None}
                        # Closing/replacing sockets during a settings change is
                        # expected; only fail unchanged descriptors.
                        if current == set(sockets):
                            for sock, source in sockets.items():
                                self._receive_failed_locked(source, sock, exc)
                    ready = []
                for sock in ready:
                    source = sockets[sock]
                    for _ in range(self.RECEIVE_BATCH):
                        with self._lock:
                            self._refresh_source_locked()
                            if sock is not self._sockets[source]:
                                break
                        try:
                            data, _peer = sock.recvfrom(65535)
                        except BlockingIOError:
                            break
                        except OSError as exc:
                            with self._lock:
                                self._receive_failed_locked(source, sock, exc)
                            break
                        self._feed_source(source, data)
            self.tick()

    def update_settings(self, settings):
        """Apply source/retention settings; startup preference is not a run command."""
        value = self._clean_settings(settings)
        with self._lock:
            if self._closed:
                return
            was_enabled = self._settings["input_enabled"]
            self._settings = value
            self._space_retry_at = 0.0
            self._refresh_source_locked(force=True)
            if not value["input_enabled"]:
                self._close_socket_locked()
                self._finish_clip_locked()
                self._clear_preroll_locked()
                self._last_packet_mono = None
                self._last_packet_at = None
                self._input_error = None
                self._source_errors = {name: None for name in self._sockets}
                self._bind_errors = {name: None for name in self._sockets}
                self._end_listeners_locked()
            elif not was_enabled and self._started:
                self._retry_at = {name: 0.0 for name in self._sockets}
                self._bind_locked()
            self._prune_locked()

    def set_recording(self, enabled):
        """Arm/disarm only this RX recorder; live listeners remain independent."""
        with self._lock:
            if self._closed:
                return
            self._recording = bool(enabled)
            if not self._recording:
                self._finish_clip_locked()
                self._clear_preroll_locked()
            else:
                self._recorder_error = None
                self._space_retry_at = 0.0
                self._prune_locked()

    def subscribe(self):
        key = uuid.uuid4().hex
        packets = queue.Queue(maxsize=self.QUEUE_CHUNKS)
        with self._lock:
            if self._closed or not self._settings["input_enabled"]:
                packets.put_nowait(None)
            else:
                if len(self._listeners) >= self.MAX_LISTENERS:
                    raise ValueError("Live audio supports up to eight simultaneous listeners")
                self._listeners[key] = packets
        return key, packets

    def unsubscribe(self, key):
        with self._lock:
            packets = self._listeners.pop(key, None)
            if packets is not None:
                self._offer(packets, None)

    @staticmethod
    def _offer(packets, data):
        try:
            packets.put_nowait(data)
        except queue.Full:
            try:
                packets.get_nowait()
            except queue.Empty:
                pass
            try:
                packets.put_nowait(data)
            except queue.Full:
                pass

    def feed_packet(self, data):
        """Ingest raw RX float32 PCM; empty datagrams are RX flush markers."""
        return self._feed_source("raw_rx", data)

    def feed_tx_packet(self, data):
        """Ingest native TX signed 16-bit PCM; publish only if TX is selected."""
        return self._feed_source("tx_mix", data)

    def _feed_source(self, source, data):
        """Validate each source independently; never sum repeated RX and TX."""
        with self._lock:
            if self._closed or not self._settings["input_enabled"]:
                return False
            self._refresh_source_locked()
            if not data:
                if source != "raw_rx":
                    self._source_errors[source] = "Ignored empty transmit PCM packet"
                    return False
                if source == self._source:
                    self._finish_clip_locked()
                    self._clear_preroll_locked()
                return True
            width = 4 if source == "raw_rx" else 2
            if len(data) % width or len(data) > 65504:
                format_name = "float32" if source == "raw_rx" else "signed 16-bit"
                self._source_errors[source] = f"Ignored malformed {source} packet (expected complete {format_name} samples)"
                return False
            input_pcm = data if source == "tx_mix" else None
            if source == "raw_rx":
                values = array("f")
                values.frombytes(data)
                if sys.byteorder != "little":
                    values.byteswap()
                if any(not math.isfinite(sample) for sample in values):
                    self._source_errors[source] = "Ignored RAWUDP packet containing non-finite samples"
                    return False
            else:
                integers = array("h")
                integers.frombytes(data)
                if sys.byteorder != "little":
                    integers.byteswap()
                values = array("f", (sample / 32768.0 for sample in integers))
                if sys.byteorder != "little":
                    values.byteswap()
                    data = values.tobytes()
                    values.byteswap()
                else:
                    data = values.tobytes()
            now = self._clock()
            self._source_last_mono[source] = now
            self._source_last_at[source] = dt.datetime.fromtimestamp(self._wall_time(), dt.timezone.utc).isoformat()
            self._source_errors[source] = None
            if source != self._source:
                return True
            if self._last_packet_mono is not None:
                gap = now - self._last_packet_mono
                if gap >= self.PREROLL_GAP_SECONDS:
                    self._clear_preroll_locked()
                timeout = self.HANG_SECONDS if source == "tx_mix" else self.NO_PACKET_SECONDS
                if gap >= timeout:
                    self._finish_clip_locked()
            self._last_packet_mono = now
            self._last_packet_at = self._source_last_at[source]
            self._input_error = None
            chunk_bytes = self.STREAM_CHUNK_FRAMES * 4
            for offset in range(0, len(data), chunk_bytes):
                chunk = data[offset:offset + chunk_bytes]
                for packets in self._listeners.values():
                    self._offer(packets, chunk)
            if not self._recording:
                return True
            clipped = [max(-1.0, min(1.0, sample)) for sample in values]
            rms = math.sqrt(sum(sample * sample for sample in clipped) / len(clipped))
            threshold = 10 ** (self._settings["recording_threshold_dbfs"] / 20)
            voiced = rms >= threshold
            if input_pcm is not None:
                raw_pcm = input_pcm
            else:
                pcm = array("h", (int(sample * 32767) for sample in clipped))
                if sys.byteorder != "little":
                    pcm.byteswap()
                raw_pcm = pcm.tobytes()
            if voiced:
                self._last_voice = now
            if self._wave is None:
                self._append_preroll_locked(raw_pcm)
                if voiced and self._begin_clip_locked(now):
                    buffered = b"".join(self._preroll)
                    self._clear_preroll_locked()
                    self._write_pcm_locked(buffered, now)
            else:
                self._write_pcm_locked(raw_pcm, now)
                if not voiced and self._last_voice is not None and now - self._last_voice >= self.HANG_SECONDS:
                    self._finish_clip_locked()
            return True

    def _append_preroll_locked(self, pcm):
        self._preroll.append(pcm)
        self._preroll_frames += len(pcm) // 2
        excess = self._preroll_frames - self.PREROLL_FRAMES
        while excess > 0 and self._preroll:
            first = self._preroll.popleft()
            frames = len(first) // 2
            if frames > excess:
                self._preroll.appendleft(first[excess * 2:])
                self._preroll_frames -= excess
                break
            self._preroll_frames -= frames
            excess -= frames

    def _clear_preroll_locked(self):
        self._preroll.clear()
        self._preroll_frames = 0

    def _has_space_locked(self, now):
        try:
            free = self._disk_usage(self.recordings_path).free
            if free < self.FREE_DISK_FLOOR:
                # Only reclaim space after crossing the free-space floor. The
                # headroom avoids resuming on a few bytes and pausing again on
                # the next packet. Use a finite snapshot and recheck real free
                # bytes: an unlinked open file may not release any blocks yet.
                target = self.FREE_DISK_FLOOR + self.FREE_DISK_BUFFER
                for _mtime, _size, path in self._closed_archives_locked():
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        continue
                    free = self._disk_usage(self.recordings_path).free
                    if free >= target:
                        break
                if free < target:
                    self._space_retry_at = now + self.SPACE_RETRY_SECONDS
                    self._recorder_error = "Recording paused: insufficient free disk space after rotating eligible RX archives"
                    return False
        except OSError as exc:
            self._space_retry_at = now + self.SPACE_RETRY_SECONDS
            self._recorder_error = f"Cannot check archive space: {exc}"
            return False
        self._space_retry_at = 0.0
        self._space_check_at = now + 1
        return True

    def _begin_clip_locked(self, now):
        if now < self._space_retry_at:
            return False
        try:
            self.recordings_path.mkdir(parents=True, exist_ok=True)
            self._prune_locked()
            if not self._has_space_locked(now):
                return False
            stamp = dt.datetime.fromtimestamp(self._wall_time(), dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
            name = f"rx_{stamp}_{uuid.uuid4().hex[:12]}.wav"
            final = self.recordings_path / name
            part = final.with_suffix(".wav.part")
            # Exclusive create protects both existing files and symlink targets.
            file = part.open("xb")
            try:
                writer = wave.open(file, "wb")
                writer.setnchannels(self.CHANNELS)
                writer.setsampwidth(2)
                writer.setframerate(self.SAMPLE_RATE)
            except Exception:
                file.close()
                part.unlink(missing_ok=True)
                raise
            self._wave = writer
            self._wave_file = file
            self._part_path, self._final_path = part, final
            self._clip_frames = 0
            self._clip_started = now
            self._recorder_error = None
            return True
        except (OSError, wave.Error) as exc:
            self._space_retry_at = now + self.SPACE_RETRY_SECONDS
            self._recorder_error = f"Cannot start RX recording: {exc}"
            return False

    def _write_pcm_locked(self, pcm, now):
        if self._wave is None:
            return
        if now >= self._space_check_at and not self._has_space_locked(now):
            self._finish_clip_locked()
            return
        maximum = self.SAMPLE_RATE * self.MAX_CLIP_SECONDS
        try:
            while pcm:
                remaining = maximum - self._clip_frames
                portion, pcm = pcm[:remaining * 2], pcm[remaining * 2:]
                self._wave.writeframesraw(portion)
                self._clip_frames += len(portion) // 2
                if self._clip_frames >= maximum:
                    self._finish_clip_locked()
                    if pcm and not self._begin_clip_locked(now):
                        break
        except (OSError, wave.Error) as exc:
            self._space_retry_at = now + self.SPACE_RETRY_SECONDS
            self._recorder_error = f"Cannot write RX recording: {exc}"
            self._finish_clip_locked()

    def _finish_clip_locked(self):
        if self._wave is None:
            return
        writer, file = self._wave, self._wave_file
        part, final, frames = self._part_path, self._final_path, self._clip_frames
        self._wave = None
        self._part_path = self._final_path = None
        self._clip_frames = 0
        self._clip_started = None
        try:
            writer.close()
            file.close()
            if frames:
                part.replace(final)
            else:
                part.unlink(missing_ok=True)
        except (OSError, wave.Error) as exc:
            self._recorder_error = f"Cannot finalize RX recording: {exc}"
            try:
                file.close()
            except OSError:
                pass
        self._prune_locked()

    def _closed_archives_locked(self):
        """A finite oldest-first snapshot of closed, exact-name regular RX files."""
        candidates = []
        try:
            for path in self.recordings_path.iterdir():
                if not self.ARCHIVE_NAME.fullmatch(path.name):
                    continue
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    candidates.append((info.st_mtime, info.st_size, path))
        except FileNotFoundError:
            pass
        return sorted(candidates)

    def _prune_locked(self):
        """Age expiry is optional; the archive size cap is always enforced."""
        try:
            candidates = self._closed_archives_locked()
            cutoff = self._wall_time() - self._settings["retention_days"] * 86400
            total = sum(size for _mtime, size, _path in candidates)
            maximum = self._settings["max_archive_mb"] * 1024 * 1024
            for mtime, size, path in candidates:
                expired = not self._settings["keep_indefinitely"] and mtime < cutoff
                if expired or total > maximum:
                    path.unlink()
                    total -= size
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._recorder_error = f"Cannot enforce RX archive retention: {exc}"

    def tick(self):
        """Finalize a clip after input stops; the receive thread also calls this."""
        with self._lock:
            if self._closed:
                return
            self._refresh_source_locked()
            now = self._clock()
            if self._last_packet_mono is not None:
                gap = now - self._last_packet_mono
                if gap >= self.PREROLL_GAP_SECONDS:
                    self._clear_preroll_locked()
                timeout = self.HANG_SECONDS if self._source == "tx_mix" else self.NO_PACKET_SECONDS
                if self._wave is not None and gap >= timeout:
                    self._finish_clip_locked()
            if now >= self._next_retention_check:
                self._prune_locked()
                self._next_retention_check = now + 60

    def status(self):
        with self._lock:
            self._refresh_source_locked()
            now = self._clock()
            receiving = (self._settings["input_enabled"] and self._last_packet_mono is not None
                         and now - self._last_packet_mono < 2)
            error = self._source_error_locked(self._source)
            includes_outgoing = (self._settings["input_enabled"] and self._source == "tx_mix"
                                 and error is None)
            sources = {name: {
                "selected": name == self._source,
                "port": self.PORT if name == "raw_rx" else self.TX_PORT,
                "format": "f32le" if name == "raw_rx" else "s16le",
                "receiving": bool(self._settings["input_enabled"] and self._source_last_mono[name] is not None
                                  and now - self._source_last_mono[name] < 2),
                "last_packet_at": self._source_last_at[name],
                "last_error": self._source_error_locked(name),
            } for name in self._sockets}
            return {"live_audio": {"configured": self._settings["input_enabled"],
                        "receiving": bool(receiving), "sample_rate": self.SAMPLE_RATE,
                        "channels": self.CHANNELS, "format": self.STREAM_FORMAT,
                        "stream_url": self.STREAM_URL, "source": self._source,
                        "includes_outgoing": includes_outgoing, "sources": sources,
                        "listeners": len(self._listeners), "last_packet_at": self._last_packet_at,
                        "last_error": error},
                    "recorder": {"enabled": self._recording, "active": self._wave is not None,
                        "current_file": self._part_path.name if self._part_path else None,
                        "last_error": self._recorder_error, "trigger": "audio_level",
                        "threshold_dbfs": self._settings["recording_threshold_dbfs"],
                        "keep_indefinitely": self._settings["keep_indefinitely"],
                        "retention_days": self._settings["retention_days"],
                        "max_archive_mb": self._settings["max_archive_mb"]}}

    def _close_socket_locked(self):
        for name, sock in self._sockets.items():
            if sock is not None:
                sock.close()
                self._sockets[name] = None
        self._socket = None

    def _end_listeners_locked(self):
        for packets in self._listeners.values():
            # A stop marker must not sit behind half a second of stale audio.
            while True:
                try:
                    packets.get_nowait()
                except queue.Empty:
                    break
            self._offer(packets, None)
        self._listeners.clear()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            self._close_socket_locked()
            self._finish_clip_locked()
            self._clear_preroll_locked()
            self._end_listeners_locked()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
