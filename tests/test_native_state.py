"""Live playback state is fresh, bounded and owned by the native engine."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
from native_state import NativeState


class NativeStateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'state.json'
        self.value = {'schema': 1, 'invocation_id': 'a' * 32, 'heartbeat_ms': 10000,
                      'online': True, 'rx_open': False, 'tx_on': False, 'control_busy': False,
                      'message_job': '', 'message_state': 'idle', 'message_sequence': 0}
        self.reader = NativeState(self.path, owner_uid=os.getuid(), clock=lambda: 11000)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o644)

    def test_current_state_is_available_but_not_a_physical_rf_claim(self):
        self.assertEqual(self.reader.read(), self.value)
        self.assertTrue(self.reader.live())
        self.value['online'] = False
        self.save()
        self.assertFalse(self.reader.live())

    def test_stale_future_wrong_tokens_types_and_extra_fields_are_rejected(self):
        for key, value in (('heartbeat_ms', 1000), ('heartbeat_ms', 20000),
                           ('invocation_id', 'not-a-token'), ('rx_open', 0),
                           ('message_job', '../file'), ('message_state', 'arbitrary'),
                           ('message_sequence', True), ('schema', True), ('extra', 'x')):
            previous = dict(self.value)
            with self.subTest(field=key, value=value):
                self.value[key] = value
                self.save()
                self.assertIsNone(self.reader.read())
                self.value = previous

    def test_missing_symlink_oversized_or_writable_state_is_unavailable(self):
        self.path.unlink()
        self.assertIsNone(self.reader.read())
        target = self.path.with_name('other.json')
        target.write_text(json.dumps(self.value))
        self.path.symlink_to(target)
        self.assertIsNone(self.reader.read())
        self.path.unlink()
        self.path.write_bytes(b'x' * 4097)
        self.assertIsNone(self.reader.read())
        self.save()
        self.path.chmod(0o666)
        self.assertIsNone(self.reader.read())
        self.save()
        self.path.parent.chmod(0o777)
        self.assertIsNone(self.reader.read())
        self.path.parent.chmod(0o700)

    def test_duplicate_json_fields_or_wrong_owner_are_unavailable(self):
        self.path.write_text('{"schema":1,"schema":1}')
        self.assertIsNone(self.reader.read())
        self.save()
        self.assertIsNone(NativeState(self.path, owner_uid=os.getuid() + 1, clock=lambda: 11000).read())


if __name__ == '__main__':
    unittest.main()
