"""Receive-only packet decoding and private storage; no hardware is opened."""
from array import array
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('data_audio_hub', ROOT / 'dashboard/audio_hub.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
AudioHub, DataDecoder = module.AudioHub, module.DataDecoder


def address(value, last=False, repeated=False):
    call, _, ssid = value.partition('-')
    return bytes(ord(character) << 1 for character in call.ljust(6)) + bytes([
        0x60 | (int(ssid or 0) << 1) | int(last) | (0x80 if repeated else 0)])


def frame(payload=b'Hello packet', path=()):
    addresses = address('APRS') + address('N0CALL-1', last=not path)
    for index, item in enumerate(path):
        addresses += address(item.rstrip('*'), last=index == len(path) - 1, repeated=item.endswith('*'))
    return addresses + b'\x03\xf0' + payload


def output(payload=b'Hello packet', path=()):
    return b'AX25HEX:' + frame(payload, path).hex().encode() + b'\n'


def samples(value=0.1):
    data = array('f', [value] * 960)
    if sys.byteorder != 'little':
        data.byteswap()
    return data.tobytes()


class OutputPipe:
    def __init__(self):
        self.items = queue.Queue()
        self.buffer = b''
        self.closed = False

    def add(self, value):
        self.items.put(value)

    def readline(self, maximum):
        if not self.buffer:
            value = self.items.get()
            if value is None:
                return b''
            self.buffer = value
        stop = self.buffer.find(b'\n') + 1
        count = min(maximum, stop or len(self.buffer))
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def close(self):
        self.closed = True
        self.items.put(None)


class InputPipe:
    def __init__(self, factory):
        self.factory = factory
        self.closed = False
        self.writes = []

    def write(self, value):
        if self.closed:
            raise BrokenPipeError()
        self.writes.append(value)
        self.factory.written.set()
        return len(value)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for process in self.factory.processes:
            process.returncode = 0
            if isinstance(process.stdout, OutputPipe):
                for line in self.factory.on_flush:
                    process.stdout.add(line)
                process.stdout.add(None)


class Process:
    def __init__(self, stdin, stdout):
        self.stdin, self.stdout = stdin, stdout
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        if isinstance(self.stdout, OutputPipe):
            self.stdout.add(None)

    def kill(self):
        self.terminate()
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('fake decoder', timeout)
        return self.returncode


class Factory:
    def __init__(self):
        self.calls = []
        self.processes = []
        self.written = threading.Event()
        self.on_flush = []

    def __call__(self, command, **options):
        self.calls.append((command, options))
        process = Process(InputPipe(self) if command[0] == '/fake/sox' else None,
                          io.BytesIO() if command[0] == '/fake/sox' else OutputPipe())
        self.processes.append(process)
        return process


class DataDecoderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub = AudioHub(self.root / 'recordings', {'input_enabled': True})
        self.factory = Factory()
        self.decoder = self.make_decoder()

    def make_decoder(self, **options):
        return DataDecoder(self.root, self.hub, enabled=True, process_factory=self.factory,
                           decoder_path='/fake/multimon-ng', sox_path='/fake/sox', **options)

    def tearDown(self):
        self.decoder.close()
        self.hub.close()
        self.tmp.cleanup()

    def accepted(self, line=None):
        return self.decoder._accept_output(output() if line is None else line, self.decoder._epoch)

    def wait_for(self, predicate):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.005)
        self.fail('Decoder did not reach the expected test state')

    def test_valid_address_path_and_exact_frame_are_exported_as_plain_data(self):
        self.assertTrue(self.accepted(output(b'Packet <script> & text', ('WIDE1-1*', 'WIDE2-2'))))
        packet = self.decoder.list_packets()[0]
        self.assertEqual(packet['source'], 'N0CALL-1')
        self.assertEqual(packet['destination'], 'APRS')
        self.assertEqual(packet['path'], ['WIDE1-1*', 'WIDE2-2'])
        self.assertEqual(packet['payload'], 'Packet <script> & text')
        self.assertEqual(bytes.fromhex(packet['frame_hex']), frame(b'Packet <script> & text', ('WIDE1-1*', 'WIDE2-2')))
        data, filename, content_type = self.decoder.export('json')
        self.assertEqual(json.loads(data)['packets'][0], packet)
        self.assertEqual((filename, content_type), ('received-packets.json', 'application/json'))
        packet['path'].clear()
        self.assertEqual(len(self.decoder.list_packets()[0]['path']), 2)

    def test_embedded_newline_nul_and_fake_decoder_prefix_remain_one_packet(self):
        payload = b'first\nAPRS: FAKE>APRS:forged\r\nAX25HEX:0000\x00\\last\xff'
        self.assertTrue(self.accepted(output(payload)))
        packets = self.decoder.list_packets()
        self.assertEqual(len(packets), 1)
        self.assertIn('\\x0aAPRS: FAKE', packets[0]['payload'])
        self.assertIn('\\x00', packets[0]['payload'])
        self.assertIn('\\xff', packets[0]['payload'])
        self.assertEqual(bytes.fromhex(packets[0]['frame_hex']), frame(payload))
        text, _, _ = self.decoder.export('txt')
        self.assertEqual(text.count(b'\n'), 1)
        self.assertNotIn(b'\x00', text)

    def test_only_bounded_framed_hex_output_is_accepted_never_dtmf_or_legacy_text(self):
        for line in (b'123456#\n', b'DTMF: 123456\n', b'APRS: N0CALL>APRS:legacy\n',
                     b'AX25HEX:zz\n', b'AX25HEX:0\n', output()[:-1], b'x' * (self.decoder.MAX_LINE_BYTES + 1)):
            self.assertFalse(self.accepted(line), line[:25])
        self.assertEqual(self.decoder.list_packets(), [])

    def test_ax25_structure_and_mode_are_checked_after_decoder_crc_boundary(self):
        good = frame()
        invalid = [good[:5], good[:14] + b'\x13\xf0' + good[16:], good[:14] + b'\x03\xcf' + good[16:],
                   b'\x01' + good[1:], good[:6] + b'\x01' + good[7:], frame(b'x' * self.decoder.MAX_FRAME_BYTES)]
        for value in invalid:
            self.assertFalse(self.accepted(b'AX25HEX:' + value.hex().encode() + b'\n'))
        self.assertEqual(self.decoder.status()['packet_count'], 0)

    def test_reader_discards_an_entire_oversized_line_and_accepts_next_frame(self):
        process = mock.Mock(stdout=io.BytesIO(b'AX25HEX:' + b'a' * (self.decoder.MAX_LINE_BYTES * 2) + b'\n' + output()))
        self.decoder._read_output(process, self.decoder._epoch)
        self.assertEqual(self.decoder.status()['packet_count'], 1)

    def test_disabled_input_and_old_process_epochs_cannot_save_packets(self):
        epoch = self.decoder._epoch
        self.decoder.set_enabled(False)
        self.assertFalse(self.accepted())
        self.decoder.set_enabled(True)
        self.assertFalse(self.decoder._accept_output(output(), epoch))
        self.hub.update_settings({'input_enabled': False})
        self.assertFalse(self.accepted())
        self.assertFalse(self.decoder.status()['input_enabled'])
        self.assertIn('feed is off', self.decoder.status()['reason'])

    def test_each_packet_is_an_atomic_private_file_and_history_survives_restart(self):
        self.assertTrue(self.accepted())
        files = list(self.decoder.directory.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.decoder.directory.stat().st_mode & 0o777, 0o700)
        restarted = self.make_decoder()
        try:
            self.assertTrue(restarted.status()['available'])
            self.assertEqual(restarted.list_packets(), self.decoder.list_packets())
        finally:
            restarted.close()

    def test_count_and_byte_limits_rotate_oldest_private_packet_files(self):
        self.decoder.MAX_PACKETS = 3
        for number in range(5):
            self.assertTrue(self.accepted(output(str(number).encode())))
        self.assertEqual([packet['payload'] for packet in self.decoder.list_packets(3)], ['4', '3', '2'])
        self.assertEqual(len(list(self.decoder.directory.glob('*.json'))), 3)
        current_size = self.decoder.status()['stored_bytes']
        self.decoder.MAX_BYTES = current_size // 2
        self.assertTrue(self.accepted(output(b'new')))
        self.assertLessEqual(self.decoder.status()['stored_bytes'], self.decoder.MAX_BYTES)
        self.assertEqual(self.decoder.list_packets(3)[0]['payload'], 'new')
        self.assertEqual(sum(path.stat().st_size for path in self.decoder.directory.glob('*.json')), self.decoder.status()['stored_bytes'])

    def test_unsafe_symlink_permissions_and_tampered_history_fail_closed(self):
        self.assertTrue(self.accepted())
        path = next(self.decoder.directory.glob('*.json'))
        for change in ('permissions', 'content', 'symlink'):
            original = path.read_bytes()
            if change == 'permissions':
                path.chmod(0o644)
            elif change == 'content':
                record = json.loads(original); record['source'] = 'FORGED'; path.write_text(json.dumps(record))
            else:
                target = self.root / 'outside'; target.write_text('private outside data')
                path.unlink(); path.symlink_to(target)
            restarted = self.make_decoder()
            try:
                self.assertFalse(restarted.status()['available'], change)
            finally:
                restarted.close()
            if path.is_symlink():
                self.assertEqual((self.root / 'outside').read_text(), 'private outside data'); path.unlink()
            path.write_bytes(original); path.chmod(0o600)

    def test_symlink_store_directory_is_never_followed(self):
        other = self.root / 'other-state'; other.mkdir()
        target = self.root / 'outside-directory'; target.mkdir()
        (other / 'data-packets').symlink_to(target)
        decoder = DataDecoder(other, self.hub, process_factory=self.factory)
        try:
            self.assertFalse(decoder.status()['available'])
            self.assertEqual(list(target.iterdir()), [])
        finally:
            decoder.close()

    def test_orphan_private_temporary_files_are_cleaned_on_restart(self):
        path = self.decoder.directory / ('.packet-' + 'a' * 32 + '.tmp')
        path.write_bytes(b'partial write'); path.chmod(0o600)
        restarted = self.make_decoder()
        try:
            self.assertTrue(restarted.status()['available'])
            self.assertFalse(path.exists())
        finally:
            restarted.close()

    def test_bad_requests_and_unavailable_tools_never_start_a_process(self):
        for value in ('csv', '../secrets', None):
            with self.assertRaises(ValueError):
                self.decoder.export(value)
        with self.assertRaises(ValueError):
            self.decoder.set_enabled(1)
        with self.assertRaises(ValueError):
            self.decoder.list_packets(1001)
        decoder = DataDecoder(self.root, self.hub, enabled=True, decoder_path='/missing/decoder')
        try:
            self.assertFalse(decoder.status()['available'])
            self.assertFalse(decoder._start_pipeline())
        finally:
            decoder.close()
        self.assertEqual(self.factory.calls, [])

    def test_stream_pipeline_is_stateful_fixed_and_does_not_open_audio_devices(self):
        self.decoder.start()
        self.hub.feed_packet(samples())
        self.assertTrue(self.factory.written.wait(1))
        self.hub.feed_packet(samples(0.2))
        self.wait_for(lambda: len(self.factory.processes[0].stdin.writes) == 2)
        self.assertEqual(len(self.factory.calls), 2)
        sox, decoder = self.factory.calls
        self.assertEqual(sox[0], ['/fake/sox', '--buffer', '2048', '-q', '-D', '-t', 'raw', '-e', 'floating-point', '-b', '32', '-L', '-r', '48000', '-c', '1', '-', '-t', 'raw', '-e', 'signed-integer', '-b', '16', '-L', '-r', '22050', '-c', '1', '-'])
        self.assertEqual(decoder[0], ['/fake/multimon-ng', '-q', '-c', '-a', 'AFSK1200', '-A', '-t', 'raw', '-'])
        for command, options in self.factory.calls:
            self.assertNotIn('shell', options)
            self.assertEqual(options['stderr'], subprocess.DEVNULL)
            self.assertFalse(any('/dev/' in argument or 'hw:' in argument for argument in command))
        self.factory.processes[1].stdout.add(output())
        self.wait_for(lambda: self.decoder.status()['packet_count'] == 1)
        self.assertTrue(self.decoder.status()['running'])

    def test_decoder_is_raw_rx_only_while_tx_mix_is_selected(self):
        self.hub._live_mode_probe = lambda: True
        self.decoder.start()
        self.hub.feed_tx_packet(array('h', [1200] * 960).tobytes())
        self.assertFalse(self.factory.written.wait(0.05))
        self.hub.feed_packet(samples())
        self.assertTrue(self.factory.written.wait(1))
        self.assertEqual(self.hub.status()['live_audio']['source'], 'tx_mix')
        self.assertEqual(self.factory.processes[0].stdin.writes, [samples()])

    def test_end_of_rx_flush_drains_the_resampler_so_final_frame_is_not_lost(self):
        self.factory.on_flush = [output(b'last frame')]
        self.decoder.start(); self.hub.feed_packet(samples())
        self.assertTrue(self.factory.written.wait(1))
        self.hub.feed_packet(b'')
        self.wait_for(lambda: self.decoder.status()['packet_count'] == 1)
        self.wait_for(lambda: not self.decoder.status()['running'])
        self.assertEqual(self.decoder.list_packets()[0]['payload'], 'last frame')

    def test_disable_and_close_terminate_pipeline_and_release_raw_subscription(self):
        self.decoder.start(); self.hub.feed_packet(samples())
        self.assertTrue(self.factory.written.wait(1))
        self.decoder.set_enabled(False)
        self.assertFalse(self.decoder.status()['running'])
        self.assertTrue(all(process.poll() is not None for process in self.factory.processes))
        self.hub.feed_packet(samples())
        self.assertEqual(len(self.factory.calls), 2)
        self.decoder.close(); self.decoder.close()
        self.assertEqual(self.hub._raw_consumers, {})

    def test_setting_the_existing_enabled_preference_keeps_the_active_packet_pipeline(self):
        self.decoder.start(); self.hub.feed_packet(samples())
        self.assertTrue(self.factory.written.wait(1))
        before, epoch = self.decoder._processes, self.decoder._epoch
        self.decoder.set_enabled(True)
        self.assertIs(self.decoder._processes, before)
        self.assertEqual(self.decoder._epoch, epoch)
        self.hub.feed_packet(samples(0.2))
        self.wait_for(lambda: len(before[0].stdin.writes) == 2)
        self.assertEqual(len(self.factory.calls), 2)

    def test_missing_tools_are_revalidated_on_enable_and_bounded_status_refresh(self):
        now = [0.0]
        with mock.patch.object(DataDecoder, '_validate_tools', return_value=False) as validator:
            decoder = DataDecoder(self.root, self.hub, enabled=False, monotonic=lambda: now[0])
            self.assertFalse(decoder.status()['available'])
            self.assertEqual(validator.call_count, 1)
            now[0] = 4.9; decoder.status(); self.assertEqual(validator.call_count, 1)
            validator.return_value = True
            now[0] = 5.0; self.assertTrue(decoder.status()['available'])
            self.assertEqual(validator.call_count, 2)
            decoder.close()
        with mock.patch.object(DataDecoder, '_validate_tools', return_value=False) as validator:
            decoder = DataDecoder(self.root, self.hub, enabled=True)
            validator.return_value = True
            decoder.set_enabled(True)
            self.assertTrue(decoder.status()['available'])
            self.assertEqual(validator.call_count, 2)
            decoder.close()

    def test_fifo_packet_file_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.decoder.directory / ('a' * 32 + '.json')
        os.mkfifo(fifo, 0o600)
        restarted = self.make_decoder()
        try:
            self.assertFalse(restarted.status()['available'])
        finally:
            restarted.close()

    def test_storage_failure_disables_further_writes_instead_of_growing_past_quota(self):
        self.assertTrue(self.accepted())
        self.decoder.MAX_PACKETS = 1
        with mock.patch.object(self.decoder, '_prune_packets_locked', side_effect=OSError('private path')):
            self.assertFalse(self.accepted(output(b'next')))
        count = len(list(self.decoder.directory.glob('*.json')))
        self.assertFalse(self.accepted(output(b'more')))
        self.assertEqual(len(list(self.decoder.directory.glob('*.json'))), count)
        self.assertFalse(self.decoder.status()['available'])
        self.assertNotIn('private path', self.decoder.status()['last_error'])

    def test_real_subprocess_pipeline_publishes_a_packet_before_continuous_stdin_is_closed(self):
        # Tiny deterministic tools exercise the real OS pipes/reader lifecycle;
        # modulation and the actual pinned decoder have a separate offline probe.
        sox = self.root / 'pipe-sox'
        sox.write_text('#!' + sys.executable + '\nimport os\nwhile True:\n data=os.read(0,4096)\n if not data: break\n os.write(1,data)\n')
        sox.chmod(0o700)
        tool = self.root / 'pipe-decoder'
        tool.write_text('#!' + sys.executable + '\nimport os\nif os.read(0,1):\n os.write(1,' + repr(output(b'continuous stream')) + ')\nwhile os.read(0,4096): pass\n')
        tool.chmod(0o700)
        self.decoder.close()
        self.decoder = DataDecoder(self.root, self.hub, enabled=True,
                                   sox_path=str(sox), decoder_path=str(tool),
                                   process_factory=subprocess.Popen)
        self.decoder.start(); self.hub.feed_packet(samples())
        self.wait_for(lambda: self.decoder.status()['packet_count'] == 1)
        self.assertEqual(self.decoder.list_packets()[0]['payload'], 'continuous stream')
        self.assertTrue(self.decoder.status()['running'])
        self.assertFalse(self.decoder._processes[0].stdin.closed)
        self.assertTrue(all(process.poll() is None for process in self.decoder._processes))

    def test_process_failure_is_sanitized_and_retry_is_bounded(self):
        self.decoder._factory = mock.Mock(side_effect=OSError('secret device path and RF text'))
        self.assertFalse(self.decoder._start_pipeline())
        self.assertEqual(self.decoder._factory.call_count, 1)
        self.assertFalse(self.decoder._start_pipeline())
        self.assertEqual(self.decoder._factory.call_count, 1)
        self.assertNotIn('secret', self.decoder.status()['last_error'])


if __name__ == '__main__':
    unittest.main()
