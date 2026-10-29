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
import hashlib
import math
import json
import os
from pathlib import Path
import queue
import re
import select
import shutil
import socket
import stat
import subprocess
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
    MAX_RAW_CONSUMERS = 2
    RAW_QUEUE_CHUNKS = 64
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
        self._raw_consumers = {}
        self._raw_last_mono = None
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
        self._reset_raw_locked()
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
            self._reset_raw_locked()
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
                self._reset_raw_locked("state", False)
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
            if value["input_enabled"] and not was_enabled:
                self._reset_raw_locked("state", True)
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

    def raw_input_enabled(self):
        with self._lock:
            return not self._closed and self._settings["input_enabled"]

    def subscribe_raw(self):
        """Private decoder tap: validated RX only, never the selected TX mix.

        Events are (kind, value, monotonic_time). A reset discards any incomplete
        demodulator state; slow consumers never hold up the audio receiver.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Audio hub has been closed")
            if len(self._raw_consumers) >= self.MAX_RAW_CONSUMERS:
                raise ValueError("Receiver data consumers are already connected")
            key = uuid.uuid4().hex
            packets = queue.Queue(maxsize=self.RAW_QUEUE_CHUNKS)
            packets.put_nowait(("state", self._settings["input_enabled"], self._clock()))
            self._raw_consumers[key] = packets
            return key, packets

    def unsubscribe_raw(self, key):
        with self._lock:
            packets = self._raw_consumers.pop(key, None)
            if packets is not None:
                self._replace_raw_event(packets, ("closed", None, self._clock()))

    @staticmethod
    def _replace_raw_event(packets, event):
        while True:
            try:
                packets.get_nowait()
            except queue.Empty:
                break
        packets.put_nowait(event)

    def _reset_raw_locked(self, kind="reset", value=None):
        self._raw_last_mono = None
        for packets in self._raw_consumers.values():
            self._replace_raw_event(packets, (kind, value, self._clock()))

    def _flush_raw_locked(self):
        self._raw_last_mono = None
        for packets in self._raw_consumers.values():
            try:
                packets.put_nowait(("flush", None, self._clock()))
            except queue.Full:
                self._replace_raw_event(packets, ("reset", None, self._clock()))

    def _publish_raw_locked(self, data, now):
        if self._raw_last_mono is not None and now - self._raw_last_mono >= self.PREROLL_GAP_SECONDS:
            self._flush_raw_locked()
        self._raw_last_mono = now
        for offset in range(0, len(data), self.STREAM_CHUNK_FRAMES * 4):
            chunk = data[offset:offset + self.STREAM_CHUNK_FRAMES * 4]
            for packets in self._raw_consumers.values():
                try:
                    packets.put_nowait(("audio", chunk, now))
                except queue.Full:
                    self._replace_raw_event(packets, ("reset", None, now))
                    packets.put_nowait(("audio", chunk, now))

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
                self._flush_raw_locked()
                if source == self._source:
                    self._finish_clip_locked()
                    self._clear_preroll_locked()
                return True
            width = 4 if source == "raw_rx" else 2
            if len(data) % width or len(data) > 65504:
                format_name = "float32" if source == "raw_rx" else "signed 16-bit"
                self._source_errors[source] = f"Ignored malformed {source} packet (expected complete {format_name} samples)"
                if source == "raw_rx":
                    self._reset_raw_locked()
                return False
            input_pcm = data if source == "tx_mix" else None
            if source == "raw_rx":
                values = array("f")
                values.frombytes(data)
                if sys.byteorder != "little":
                    values.byteswap()
                if any(not math.isfinite(sample) for sample in values):
                    self._source_errors[source] = "Ignored RAWUDP packet containing non-finite samples"
                    self._reset_raw_locked()
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
            if source == "raw_rx":
                self._publish_raw_locked(data, now)
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
            self._reset_raw_locked("closed")
            self._raw_consumers.clear()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)


class DataDecoder:
    """Receive-only APRS text packets from the existing raw RX audio tap.

    The installed decoder emits one hex-encoded, CRC-checked AX.25 frame per
    line. No RF payload is interpreted as a command or a filesystem name.
    Resampling, demodulation and storage run outside AudioHub's audio lock.
    """
    DECODER_PATH = "/opt/repeater-box/vendor/multimon-ng/multimon-ng"
    SOX_PATH = "/usr/bin/sox"
    MODE = "APRS / AX.25 AFSK1200"
    SOURCE_COMMIT = "83761b00d5dd181171b28e8cd2f93b6983fadbc0"
    SOURCE_SHA256 = "39c537af9d4fb1d35c8d9bc26fd3c6d0b521440c45059b26da4758765c175b82"
    PATCH_SHA256 = "c20aa9eb27561f524058af241481a3cb2b41da3f42f4ebc2839fd56267e4bae3"
    MAX_PACKETS = 1000
    MAX_BYTES = 8 * 1024 * 1024
    MAX_FRAME_BYTES = 2048
    MAX_LINE_BYTES = len(b"AX25HEX:") + MAX_FRAME_BYTES * 2 + 1
    MAX_RECORD_BYTES = 40 * 1024
    ID = re.compile(r"[0-9a-f]{32}\Z")
    RECORD_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
    CALL = re.compile(r"[A-Z0-9]{1,6}\Z")
    RETRY_SECONDS = 5.0

    def __init__(self, state_dir, audio_hub, *, enabled=False,
                 decoder_path=DECODER_PATH, sox_path=SOX_PATH,
                 process_factory=None, monotonic=None, wall_time=None):
        self.directory = Path(state_dir) / "data-packets"
        self.hub = audio_hub
        self.decoder_path = str(decoder_path)
        self.sox_path = str(sox_path)
        self._factory = process_factory or subprocess.Popen
        self._clock = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._enabled = enabled is True
        self._closed = False
        self._thread = None
        self._subscription = None
        self._queue = None
        self._processes = None
        self._reader = None
        self._epoch = 0
        self._retry_at = 0.0
        self._last_audio = None
        self._accept_after = self._clock()
        self._error = None
        self._packets = deque()
        self._sizes = {}
        self._total_bytes = 0
        self._store_ready = False
        self._tools_ready = process_factory is not None or self._validate_tools()
        self._tools_check_at = self._clock() + self.RETRY_SECONDS
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_directory()
            self._load_packets()
            self._store_ready = True
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            self._error = "The private received-packet store is unavailable."

    @staticmethod
    def _read_owned(path, *, owner, maximum, executable=False):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1 or info.st_size > maximum:
                raise ValueError("Unsafe decoder file")
            if info.st_mode & (0o022 if executable else 0o077):
                raise ValueError("Unsafe decoder permissions")
            if executable and not info.st_mode & 0o111:
                raise ValueError("Decoder is not executable")
            with os.fdopen(fd, "rb") as stream:
                fd = None
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError("Decoder file exceeds its limit")
            return raw
        finally:
            if fd is not None:
                os.close(fd)

    def _validate_tools(self):
        try:
            decoder = self._read_owned(self.decoder_path, owner=0, maximum=16 * 1024 * 1024, executable=True)
            self._read_owned(self.sox_path, owner=0, maximum=16 * 1024 * 1024, executable=True)
            metadata_path = Path(self.decoder_path).with_name("metadata.json")
            # Root-owned public metadata is readable, but must never be writable
            # by the dashboard account or followed through a symlink.
            info = metadata_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 4096:
                return False
            fd = os.open(metadata_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                metadata = json.loads(stream.read(4097))
            expected = {"schema": 1, "version": "1.6.1", "source_commit": self.SOURCE_COMMIT,
                        "source_sha256": self.SOURCE_SHA256, "input": "stdin-only",
                        "output_format": "ax25-hex-v1", "patch_sha256": self.PATCH_SHA256,
                        "binary_sha256": hashlib.sha256(decoder).hexdigest()}
            return metadata == expected
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _check_directory(self):
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe packet directory")

    @staticmethod
    def _escaped(raw):
        return "".join(chr(value) if 32 <= value <= 126 and value != 92
                       else "\\\\" if value == 92 else f"\\x{value:02x}" for value in raw)

    @classmethod
    def frame_values(cls, frame):
        """Parse supported AX.25 UI/PID-F0 frames; FCS was checked by the decoder."""
        if not isinstance(frame, bytes) or not 16 <= len(frame) <= cls.MAX_FRAME_BYTES:
            raise ValueError("Unsupported AX.25 frame")
        addresses = []
        offset = 0
        for _ in range(10):  # Destination, source, at most eight repeaters.
            address = frame[offset:offset + 7]
            if len(address) != 7 or any(value & 1 for value in address[:6]) or address[6] & 0x60 != 0x60:
                raise ValueError("Invalid AX.25 address")
            call = bytes(value >> 1 for value in address[:6]).decode("ascii").rstrip(" ")
            if not cls.CALL.fullmatch(call):
                raise ValueError("Invalid AX.25 callsign")
            ssid = (address[6] >> 1) & 15
            addresses.append((call + (f"-{ssid}" if ssid else ""), bool(address[6] & 0x80)))
            offset += 7
            if address[6] & 1:
                break
        else:
            raise ValueError("AX.25 address list is too long")
        if len(addresses) < 2 or frame[offset:offset + 2] != b"\x03\xf0":
            raise ValueError("Only APRS UI packets are supported")
        destination, source = addresses[0][0], addresses[1][0]
        path = [call + ("*" if repeated else "") for call, repeated in addresses[2:]]
        payload = cls._escaped(frame[offset + 2:])
        raw = source + ">" + destination + ("," + ",".join(path) if path else "") + ":" + payload
        return {"source": source, "destination": destination, "path": path,
                "payload": payload, "raw": raw, "frame_hex": frame.hex()}

    def _load_packets(self):
        entries = []
        with os.scandir(self.directory) as folder:
            for count, entry in enumerate(folder, 1):
                if count > self.MAX_PACKETS * 2 + 16:
                    raise ValueError("Packet directory exceeds its limit")
                if re.fullmatch(r"\.packet-[0-9a-f]{32}\.tmp", entry.name):
                    temporary = self.directory / entry.name
                    self._read_owned(temporary, owner=os.getuid(), maximum=self.MAX_RECORD_BYTES)
                    temporary.unlink()
                    continue
                if not self.RECORD_NAME.fullmatch(entry.name):
                    continue
                path = self.directory / entry.name
                raw = self._read_owned(path, owner=os.getuid(), maximum=self.MAX_RECORD_BYTES)
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {"id", "time", "source", "destination", "path", "payload", "raw", "frame_hex"}:
                    raise ValueError("Invalid saved packet")
                if value["id"] + ".json" != entry.name or not self.ID.fullmatch(value["id"]):
                    raise ValueError("Invalid saved packet ID")
                if not isinstance(value["time"], str) or len(value["time"]) > 40:
                    raise ValueError("Invalid packet timestamp")
                timestamp = dt.datetime.fromisoformat(value["time"])
                if timestamp.tzinfo is None:
                    raise ValueError("Packet timestamp must include its zone")
                decoded = self.frame_values(bytes.fromhex(value["frame_hex"]))
                if any(value[key] != item for key, item in decoded.items()):
                    raise ValueError("Saved packet does not match its frame")
                entries.append((timestamp.timestamp(), value["id"], value, len(raw)))
        for _, identifier, value, size in sorted(entries):
            self._packets.append(value)
            self._sizes[identifier] = size
            self._total_bytes += size
        self._prune_packets_locked()

    def _prune_packets_locked(self):
        while len(self._packets) > self.MAX_PACKETS or self._total_bytes > self.MAX_BYTES:
            oldest = self._packets[0]
            path = self.directory / (oldest["id"] + ".json")
            self._read_owned(path, owner=os.getuid(), maximum=self.MAX_RECORD_BYTES)
            path.unlink()
            self._packets.popleft()
            self._total_bytes -= self._sizes.pop(oldest["id"])

    def _save_packet_locked(self, record):
        self._check_directory()
        raw = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > self.MAX_RECORD_BYTES:
            raise ValueError("Packet record exceeds its limit")
        temporary = self.directory / (".packet-" + uuid.uuid4().hex + ".tmp")
        path = self.directory / (record["id"] + ".json")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._packets.append(record)
            self._sizes[record["id"]] = len(raw)
            self._total_bytes += len(raw)
            self._prune_packets_locked()
        finally:
            temporary.unlink(missing_ok=True)

    def _accept_output(self, line, epoch):
        if not isinstance(line, bytes) or not line.endswith(b"\n") or len(line) > self.MAX_LINE_BYTES:
            return False
        try:
            if not line.startswith(b"AX25HEX:"):
                return False
            encoded = line[8:-1]
            if not encoded or re.fullmatch(b"[0-9a-fA-F]+", encoded) is None or len(encoded) % 2:
                return False
            values = self.frame_values(bytes.fromhex(encoded.decode("ascii")))
        except (ValueError, UnicodeError):
            return False
        if not self.hub.raw_input_enabled():
            return False
        with self._lock:
            if self._closed or not self._enabled or epoch != self._epoch or not self._store_ready:
                return False
            record = {"id": uuid.uuid4().hex,
                      "time": dt.datetime.fromtimestamp(self._wall_time(), dt.timezone.utc).isoformat(), **values}
            try:
                self._save_packet_locked(record)
                self._error = None
                return True
            except (OSError, ValueError):
                self._error = "A received packet could not be saved safely."
                self._store_ready = False
                return False

    def _read_output(self, process, epoch):
        try:
            while not self._stop.is_set():
                line = process.stdout.readline(self.MAX_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > self.MAX_LINE_BYTES or not line.endswith(b"\n"):
                    while line and not line.endswith(b"\n"):
                        line = process.stdout.readline(self.MAX_LINE_BYTES + 1)
                    continue
                self._accept_output(line, epoch)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _dispose(processes):
        for process in processes or ():
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=0.3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=0.3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    def _end_pipeline(self, drain=False):
        with self._lock:
            processes, reader = self._processes, self._reader
            if not drain:
                self._epoch += 1
                self._processes = self._reader = None
        if processes is None:
            return
        if drain:
            try:
                processes[0].stdin.close()
                for process in processes:
                    process.wait(timeout=0.5)
                if reader is not None and reader is not threading.current_thread():
                    reader.join(timeout=0.5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        with self._lock:
            if self._processes is processes:
                self._processes = self._reader = None
                self._epoch += 1
        self._dispose(processes)

    def _start_pipeline(self):
        with self._lock:
            epoch = self._epoch
            if self._closed or not self._enabled or not self._tools_ready or not self._store_ready or self._clock() < self._retry_at:
                return False
        sox = decoder = None
        try:
            options = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                       "bufsize": 0, "close_fds": True,
                       "env": {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}}
            sox = self._factory([self.sox_path, "--buffer", "2048", "-q", "-D",
                "-t", "raw", "-e", "floating-point", "-b", "32", "-L", "-r", "48000", "-c", "1", "-",
                "-t", "raw", "-e", "signed-integer", "-b", "16", "-L", "-r", "22050", "-c", "1", "-"],
                stdin=subprocess.PIPE, **options)
            decoder = self._factory([self.decoder_path, "-q", "-c", "-a", "AFSK1200", "-A", "-t", "raw", "-"],
                                    stdin=sox.stdout, **options)
            sox.stdout.close()
            with self._lock:
                if self._closed or not self._enabled or self._epoch != epoch or not self.hub.raw_input_enabled():
                    self._dispose((sox, decoder))
                    return False
                self._processes = (sox, decoder)
                self._reader = threading.Thread(target=self._read_output, args=(decoder, epoch),
                                                name="repeater-data-output", daemon=True)
                self._reader.start()
                self._error = None
            return True
        except (OSError, ValueError):
            self._dispose(tuple(process for process in (sox, decoder) if process is not None))
            with self._lock:
                self._error = "The receive-only packet decoder could not start."
                self._retry_at = self._clock() + self.RETRY_SECONDS
            return False

    def _run(self):
        while not self._stop.is_set():
            try:
                kind, data, received = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._last_audio is not None and self._clock() - self._last_audio >= AudioHub.PREROLL_GAP_SECONDS:
                    self._end_pipeline(drain=True)
                    self._last_audio = None
                continue
            if kind == "closed":
                break
            if kind != "audio":
                self._end_pipeline(drain=kind == "flush")
                self._last_audio = None
                continue
            with self._lock:
                enabled = self._enabled and not self._closed and self._tools_ready and self._store_ready
                processes = self._processes
            if not enabled or not self.hub.raw_input_enabled():
                if processes is not None:
                    self._end_pipeline()
                continue
            if received < self._accept_after or self._clock() - received > 2:
                self._end_pipeline()
                continue
            if processes is not None and any(process.poll() is not None for process in processes):
                self._end_pipeline()
                with self._lock:
                    self._error = "The receive-only packet decoder stopped. Retrying automatically."
                    self._retry_at = self._clock() + self.RETRY_SECONDS
                continue
            if processes is None:
                if not self._start_pipeline():
                    continue
                with self._lock:
                    processes = self._processes
            if processes is None:
                continue
            try:
                if processes[0].stdin.write(data) != len(data):
                    raise OSError("Incomplete audio write")
                self._last_audio = self._clock()
            except (OSError, ValueError):
                self._end_pipeline()
                with self._lock:
                    self._error = "Receiver data audio was interrupted."
                    self._retry_at = self._clock() + self.RETRY_SECONDS
        self._end_pipeline()

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Data decoder has been closed")
            if self._thread is not None:
                return
            self._subscription, self._queue = self.hub.subscribe_raw()
            self._thread = threading.Thread(target=self._run, name="repeater-data", daemon=True)
            self._thread.start()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Data decoding must be true or false")
        with self._lock:
            if self._closed:
                return
            if enabled == self._enabled and (not enabled or self._tools_ready):
                return
            recheck = enabled and not self._tools_ready
        tools_ready = self._validate_tools() if recheck else None
        with self._lock:
            if self._closed:
                return
            if tools_ready is not None:
                self._tools_ready = tools_ready
                self._tools_check_at = self._clock() + self.RETRY_SECONDS
            self._enabled = enabled
            self._retry_at = 0.0
            self._accept_after = self._clock()
        self._end_pipeline()
        # Discard audio queued before the operator changed the preference.
        if self._queue is not None:
            for _ in range(AudioHub.RAW_QUEUE_CHUNKS):
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def status(self):
        with self._lock:
            recheck = not self._closed and not self._tools_ready and self._clock() >= self._tools_check_at
            if recheck:
                self._tools_check_at = self._clock() + self.RETRY_SECONDS
        if recheck:
            ready = self._validate_tools()
            with self._lock:
                if not self._closed:
                    self._tools_ready = ready
        input_enabled = self.hub.raw_input_enabled()
        with self._lock:
            available = self._tools_ready and self._store_ready and not self._closed
            running = bool(available and self._enabled and input_enabled and self._processes is not None
                           and all(process.poll() is None for process in self._processes))
            reason = ("Install the receive-only data decoder on the Pi first." if not self._tools_ready
                      else "The private received-packet store is unavailable." if not self._store_ready
                      else "Receiver audio feed is off." if not input_enabled
                      else "Data decoding is off." if not self._enabled else "")
            return {"available": available, "enabled": self._enabled, "running": running,
                    "input_enabled": input_enabled, "mode": self.MODE,
                    "packet_count": len(self._packets), "stored_bytes": self._total_bytes,
                    "last_packet_at": self._packets[-1]["time"] if self._packets else None,
                    "last_error": self._error, "reason": reason,
                    "limits": {"max_packets": self.MAX_PACKETS, "max_bytes": self.MAX_BYTES}}

    def list_packets(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= self.MAX_PACKETS:
            raise ValueError("Invalid packet list limit")
        with self._lock:
            return [{**record, "path": list(record["path"])} for record in list(self._packets)[-limit:][::-1]]

    def export(self, format="json"):
        if format not in ("json", "txt"):
            raise ValueError("Choose JSON or text export")
        packets = self.list_packets(self.MAX_PACKETS)
        if format == "json":
            payload = {"schema": 1, "mode": self.MODE, "packets": packets}
            return ((json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode(),
                    "received-packets.json", "application/json")
        raw = "\n".join(record["time"] + " " + record["raw"] for record in packets) + ("\n" if packets else "")
        return raw.encode("utf-8"), "received-packets.txt", "text/plain; charset=utf-8"

    def clear(self):
        with self._lock:
            self._check_directory()
            for record in tuple(self._packets):
                path = self.directory / (record["id"] + ".json")
                self._read_owned(path, owner=os.getuid(), maximum=self.MAX_RECORD_BYTES)
                path.unlink()
            self._packets.clear()
            self._sizes.clear()
            self._total_bytes = 0

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            key, thread = self._subscription, self._thread
            self._subscription = None
        if key is not None:
            self.hub.unsubscribe_raw(key)
        self._end_pipeline()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
