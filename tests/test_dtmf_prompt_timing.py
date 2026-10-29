"""DTMF idle windows start after trusted prompt completion and cannot be replayed."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
from controls import write_json
from test_dtmf_control import CODE, RECORD, FakeConnection, fixture
SPEC = importlib.util.spec_from_file_location('prompt_timing_client', ROOT / 'scripts/dtmf-client.py')
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


class DtmfPromptTimingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.server, self.bridge, self.radio, self.maintenance, self.recording, self.calls = fixture(self.folder)
        self.addCleanup(self.bridge.close)
        self.now = [100.0]
        self.bridge.clock = lambda: self.now[0]
        self.bridge.prompt_feedback = lambda: True
        self.server.settings.update({'dtmf_enabled': True})

    def command(self, value=CODE, uid=123):
        return self.bridge.command({'schema': 1, 'command': value}, uid)

    def ack(self, token, uid=123):
        return self.bridge.command({'schema': 1, 'prompt_finished': token}, uid)

    def test_complete_late_prompt_grants_full_idle_window_then_valid_option_resets_it(self):
        opening = self.command()
        self.assertRegex(opening['prompt_id'], r'^[0-9a-f]{32}$')
        self.assertEqual(self.bridge.rf_session['expires'], 145)
        # The native prompt can have waited for earlier radio activity. Its
        # completion starts a full thirty seconds, not the old login deadline.
        self.now[0] = 142
        self.assertEqual(self.ack(opening['prompt_id']), {'schema': 1, 'accepted': True})
        self.assertEqual(self.bridge.rf_session['expires'], 172)
        self.now[0] = 171.9
        option = self.command('2')
        self.assertEqual(option['result'], 'choose_recording')
        self.assertNotEqual(option['prompt_id'], opening['prompt_id'])
        self.assertEqual(self.bridge.rf_session['expires'], 216.9)
        self.now[0] = 180
        self.assertTrue(self.ack(option['prompt_id'])['accepted'])
        self.assertEqual(self.bridge.rf_session['expires'], 210)
        self.now[0] = 209.9
        result = self.command('1')
        self.assertEqual(result['result'], 'recording_on')
        self.assertEqual(result['prompt_id'], '')
        self.assertIsNone(self.bridge.rf_session)
        self.assertFalse(self.ack(option['prompt_id'])['accepted'])
        self.assertEqual(self.command('1')['result'], 'denied')

    def test_missing_completion_has_a_fixed_45_second_fallback(self):
        opening = self.command()
        self.now[0] = 145
        self.assertFalse(self.ack(opening['prompt_id'])['accepted'])
        self.assertIsNone(self.bridge.rf_session)
        self.assertEqual(self.command('2')['result'], 'denied')
        self.command()
        self.now[0] += 45
        self.assertEqual(self.command('2')['result'], 'expired')
        self.assertIsNone(self.bridge.rf_session)

    def test_duplicate_stale_and_different_peer_callbacks_never_extend_the_window(self):
        opening = self.command()
        self.now[0] = 110
        self.assertTrue(self.ack(opening['prompt_id'])['accepted'])
        self.now[0] = 125
        self.assertFalse(self.ack(opening['prompt_id'])['accepted'])
        self.assertEqual(self.bridge.rf_session['expires'], 140)
        self.now[0] = 135
        option = self.command('3')
        self.assertEqual(self.bridge.rf_session['expires'], 180)
        self.now[0] = 150
        self.assertFalse(self.ack(opening['prompt_id'])['accepted'])
        self.assertFalse(self.ack(option['prompt_id'], uid=0)['accepted'])
        self.assertEqual(self.bridge.rf_session['expires'], 180)
        self.assertTrue(self.ack(option['prompt_id'])['accepted'])
        self.assertEqual(self.bridge.rf_session['expires'], 180)
        self.now[0] = 170
        self.assertFalse(self.ack(option['prompt_id'])['accepted'])
        self.assertEqual(self.bridge.rf_session['expires'], 180)
        self.now[0] = 180
        self.assertEqual(self.command('0')['result'], 'expired')

    def test_invalid_cancelled_and_completed_menus_cannot_be_extended_by_feedback(self):
        for value, expected in (('8', 'invalid'), ('*', 'cancelled'), ('0', 'status')):
            with self.subTest(value=value):
                opening = self.command()
                self.now[0] += 5
                self.assertEqual(self.command(value)['result'], expected)
                self.assertIsNone(self.bridge.rf_session)
                self.assertFalse(self.ack(opening['prompt_id'])['accepted'])
        opening = self.command()
        original_deadline = self.bridge.rf_session['expires']
        self.now[0] += 10
        self.assertEqual(self.command(True)['result'], 'denied')
        self.assertEqual(self.bridge.rf_session['expires'], original_deadline)
        self.assertTrue(self.ack(opening['prompt_id'])['accepted'])

    def test_without_live_feedback_each_valid_stage_has_a_new_immediate_idle_window(self):
        self.bridge.prompt_feedback = False
        self.assertEqual(self.command()['prompt_id'], '')
        self.assertEqual(self.bridge.rf_session['expires'], 130)
        self.now[0] = 129
        self.assertEqual(self.command('2')['prompt_id'], '')
        self.assertEqual(self.bridge.rf_session['expires'], 159)
        self.now[0] = 158.9
        self.assertEqual(self.command('1')['result'], 'recording_on')
        self.assertEqual(self.calls, [True])

    def test_live_feedback_is_probed_for_each_prompt_and_probe_failure_has_bounded_fallback(self):
        opening = self.command()
        self.bridge.prompt_feedback = lambda: False
        self.now[0] = 105
        self.assertEqual(self.command('2')['prompt_id'], '')
        self.assertEqual(self.bridge.rf_session['expires'], 135)
        self.assertFalse(self.ack(opening['prompt_id'])['accepted'])
        self.bridge.prompt_feedback = mock.Mock(side_effect=OSError('private fixture detail'))
        response = self.command()
        self.assertEqual(response['prompt_id'], '')
        self.assertEqual(self.bridge.rf_session['expires'], 135)
        self.assertNotIn('private fixture detail', json.dumps(response))

    def test_callback_requires_exact_grammar_trusted_peer_and_current_record(self):
        opening = self.command()
        token = opening['prompt_id']
        for changed in ({'schema': True, 'prompt_finished': token},
                        {'schema': 1, 'prompt_finished': token, 'command': '1'},
                        {'schema': 1, 'prompt_finished': ''},
                        {'schema': 1, 'prompt_finished': 'A' * 32},
                        {'schema': 1, 'prompt_finished': 1},
                        {'schema': 1, 'prompt_finished': token + ';exec nope'}):
            with self.subTest(changed=changed), mock.patch('dtmf_control.hashlib.pbkdf2_hmac') as derive:
                self.assertEqual(self.bridge.command(changed, 123), {'schema': 1, 'accepted': False})
                derive.assert_not_called()
            self.assertEqual(self.bridge.rf_session['expires'], 145)
        self.assertFalse(self.ack(token, uid=999)['accepted'])
        write_json(self.bridge.config_file, {**RECORD, 'salt': '22' * 16}, mode=0o640)
        self.assertFalse(self.ack(token)['accepted'])
        self.assertIsNone(self.bridge.rf_session)

    def test_callback_does_not_outlive_disabled_control_or_pending_maintenance(self):
        for mode in ('disabled', 'maintenance'):
            with self.subTest(mode=mode):
                self.server.settings.update({'dtmf_enabled': True})
                self.maintenance['pending'] = False
                opening = self.command()
                if mode == 'disabled':
                    self.server.settings.update({'dtmf_enabled': False})
                else:
                    self.maintenance['pending'] = True
                self.assertFalse(self.ack(opening['prompt_id'])['accepted'])
                self.assertIsNone(self.bridge.rf_session)

    def test_callback_is_serialized_with_radio_commands_and_socket_reply_is_narrow(self):
        opening = self.command()
        self.bridge.rf_command_lock.acquire()
        try:
            self.assertFalse(self.ack(opening['prompt_id'])['accepted'])
            self.assertEqual(self.bridge.rf_session['expires'], 145)
        finally:
            self.bridge.rf_command_lock.release()
        self.bridge.peer_uid = lambda connection: 123
        payload = {'schema': 1, 'prompt_finished': opening['prompt_id']}
        connection = FakeConnection(json.dumps(payload).encode() + b'\n')
        self.bridge._connection(connection)
        self.assertEqual(json.loads(connection.sent), {'schema': 1, 'accepted': True})
        self.assertNotIn(CODE.encode(), connection.sent)


class DtmfPromptClientTests(unittest.TestCase):
    def response(self, token=''):
        return {'schema': 1, 'result': 'authenticated', 'pair_index': 1, 'recording': False,
                'announce_status': False, 'identification_enabled': True, 'prompt_id': token}

    def test_event_carries_only_empty_marker_or_valid_prompt_identifier(self):
        self.assertEqual(client.response_event(self.response()),
                         b'EVENT RepeaterLogic::box_control_result authenticated 1 0 0 1 -\n')
        token = 'a1' * 16
        self.assertEqual(client.response_event(self.response(token)),
                         ('EVENT RepeaterLogic::box_control_result authenticated 1 0 0 1 ' + token + '\n').encode())
        for invalid in ('-', 'A' * 32, 'a' * 31, 'a' * 33, 'x;exec nope', None, 0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                client.validate_response(self.response(invalid))
        incomplete = self.response(); incomplete.pop('prompt_id')
        with self.assertRaises(ValueError):
            client.validate_response(incomplete)

    def test_callback_request_validates_exact_response_and_never_announces(self):
        connection = mock.MagicMock(); connection.__enter__.return_value = connection
        connection.recv.return_value = b'{"schema":1,"accepted":true}\n'
        token = 'ab' * 16
        with mock.patch.object(client.socket, 'socket', return_value=connection):
            self.assertTrue(client.prompt_finished(token)['accepted'])
        self.assertEqual(json.loads(connection.sendall.call_args.args[0]),
                         {'schema': 1, 'prompt_finished': token})
        for invalid in ({'schema': True, 'accepted': True}, {'schema': 1, 'accepted': 1},
                        {'schema': 1, 'accepted': False, 'result': 'status'}, {}):
            with self.assertRaises(ValueError):
                client.validate_prompt_ack(invalid)
        for invalid in ('', '-', 'A' * 32, '0' * 31, 'ab' * 16 + '\n'):
            with self.assertRaises(ValueError):
                client.prompt_finished(invalid)
        with mock.patch.object(client, 'prompt_finished', return_value={'schema': 1, 'accepted': False}) as sent, \
                mock.patch.object(client, 'announce') as announce, \
                mock.patch.object(client.sys, 'stdin') as stdin:
            self.assertEqual(client.main(['--prompt-finished', token]), 0)
            sent.assert_called_once_with(token)
            announce.assert_not_called()
            stdin.buffer.readline.assert_not_called()

    def test_callback_cli_errors_are_silent_and_bad_arguments_do_not_read_stdin(self):
        for arguments in (['--prompt-finished'], ['--prompt-finished', 'bad'], ['--other', 'a' * 32]):
            with self.subTest(arguments=arguments), mock.patch.object(client.sys, 'stdin') as stdin, \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(client.main(arguments), 1)
                stdin.buffer.readline.assert_not_called()
                self.assertEqual(out.getvalue() + err.getvalue(), '')
        with mock.patch.object(client, 'prompt_finished', side_effect=OSError('private fixture detail')), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(client.main(['--prompt-finished', 'a' * 32]), 1)
            self.assertEqual(out.getvalue() + err.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
