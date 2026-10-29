#!/usr/bin/python3
"""Consume one reviewed audio-message request; never queue or replay RF events."""
from datetime import datetime, timezone
import fcntl
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time

SPEC = importlib.util.spec_from_file_location('message_live_guard', Path(__file__).with_name('check-live-config.py'))
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
STATE_DIR = Path('/var/lib/repeater-dashboard')
MESSAGES_DIR = Path('/var/lib/repeater-messages')
PLAYBACK_DIR = Path('/run/repeater-message-playback')
CONSUMED_DIR = Path('/var/lib/repeater-message-dispatcher')
NATIVE_PATH = Path('/run/repeater-message-status/state.json')
COMMAND_PATH = Path('/run/repeater-box/svxlink-command')
LOCK_PATH = Path('/opt/repeater-box/.maintenance.lock')
REQUEST_NAME = 'message-broadcast-request.json'
RESULT_NAME = 'message-broadcast-result.json'
TOKEN = re.compile(r'[0-9a-f]{32}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
PAIR = re.compile(r'[a-z][a-z0-9_-]{0,15}\Z')
DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z')
NATIVE_PHASES = frozenset(('idle', 'accepted', 'playing', 'finished', 'rejected_busy', 'failed'))
PENDING = frozenset(('preparing', 'accepted', 'playing'))
TEXT = {'preparing': 'Checking the reviewed message and live repeater.',
        'accepted': 'Message accepted by the live repeater.', 'playing': 'Message playback is active.',
        'finished': 'Message playback finished.',
        'rejected': 'The live repeater could not accept this message. Review its current state before trying again.',
        'failed': 'Message playback could not be confirmed. Review live repeater status before trying again.',
        'cancelled': 'Message request cancelled.'}
MAX_FRAMES = 30 * 16000
MAX_WAV = 44 + MAX_FRAMES * 2


class DispatchError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DispatchError('Duplicate private state field.')
        result[key] = value
    return result


def protected_directory(path, owner_uid, *, mode=None):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o027
            or mode is not None and stat.S_IMODE(info.st_mode) != mode):
        raise DispatchError('Unsafe private message directory.')


def read_bytes(path, owner_uid, maximum, *, missing=False):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o027
                or not 0 < info.st_size <= maximum):
            raise DispatchError('Unsafe private message file.')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum or len(raw) != info.st_size:
            raise DispatchError('Private message file changed.')
        return raw
    except FileNotFoundError:
        if missing:
            return None
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_json(path, owner_uid, maximum, *, missing=False):
    raw = read_bytes(path, owner_uid, maximum, missing=missing)
    return None if raw is None else json.loads(raw, object_pairs_hook=unique_object)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value, owner_uid, group_id, parent_uid, mode=0o640):
    protected_directory(path.parent, parent_uid)
    read_bytes(path, owner_uid, 65536, missing=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.message-result-', dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, owner_uid, group_id)
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            descriptor = None
            json.dump(value, stream, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def timestamp(value):
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise DispatchError('Invalid request timestamp.')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


def valid_request(value):
    fields = {'schema', 'request_id', 'action', 'message_id', 'sha256', 'pair_id',
              'catalog_digest', 'invocation_id', 'requested_at'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema']) is not int
            or value['schema'] != 1 or value['action'] != 'broadcast'
            or any(not isinstance(value[key], str) or not TOKEN.fullmatch(value[key])
                   for key in ('request_id', 'message_id', 'invocation_id'))
            or any(not isinstance(value[key], str) or not HASH.fullmatch(value[key])
                   for key in ('sha256', 'catalog_digest'))
            or not isinstance(value['pair_id'], str) or not PAIR.fullmatch(value['pair_id'])):
        raise DispatchError('Invalid message request.')
    timestamp(value['requested_at'])
    return value


def valid_result(value):
    fields = {'schema', 'request_id', 'message_id', 'phase', 'success', 'message',
              'invocation_id', 'sha256', 'updated_at'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema']) is not int
            or value['schema'] != 1
            or any(not isinstance(value[key], str) or not TOKEN.fullmatch(value[key])
                   for key in ('request_id', 'message_id', 'invocation_id'))
            or not isinstance(value['sha256'], str) or not HASH.fullmatch(value['sha256'])
            or not isinstance(value['phase'], str) or value['phase'] not in TEXT
            or value['message'] != TEXT[value['phase']]):
        raise DispatchError('Invalid message result.')
    expected = True if value['phase'] == 'finished' else None if value['phase'] in PENDING else False
    if value['success'] is not expected:
        raise DispatchError('Invalid message result.')
    timestamp(value['updated_at'])
    return value


def canonical_asset(request, messages_dir, dashboard_uid):
    protected_directory(messages_dir, dashboard_uid)
    index = read_json(messages_dir / 'index.json', dashboard_uid, 32768)
    if (not isinstance(index, dict) or set(index) != {'schema', 'messages'}
            or type(index['schema']) is not int or index['schema'] != 1
            or not isinstance(index['messages'], list) or not 1 <= len(index['messages']) <= 10):
        raise DispatchError('Invalid message library.')
    seen, selected = set(), None
    for item in index['messages']:
        if (not isinstance(item, dict) or set(item) != {'id', 'sha256', 'name', 'frames', 'bytes', 'created_at'}
                or not isinstance(item['id'], str) or not TOKEN.fullmatch(item['id']) or item['id'] in seen
                or not isinstance(item['sha256'], str) or not HASH.fullmatch(item['sha256'])
                or type(item['frames']) is not int or not 0 < item['frames'] <= MAX_FRAMES
                or type(item['bytes']) is not int or item['bytes'] != 44 + item['frames'] * 2
                or not isinstance(item['name'], str) or not 1 <= len(item['name']) <= 128
                or len(item['name'].encode('utf-8')) > 256 or item['name'] != item['name'].strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in item['name'])
                or '/' in item['name'] or '\\' in item['name']
                or item['name'].rsplit('.', 1)[-1].lower() not in ('wav', 'mp3', 'm4a')):
            raise DispatchError('Invalid message library.')
        timestamp(item['created_at'])
        seen.add(item['id'])
        if item['id'] == request['message_id']:
            selected = item
    if selected is None or selected['sha256'] != request['sha256']:
        raise DispatchError('The reviewed message is unavailable.')
    raw = read_bytes(messages_dir / (request['message_id'] + '.wav'), dashboard_uid, MAX_WAV)
    pcm_bytes = selected['frames'] * 2
    header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + pcm_bytes, b'WAVE', b'fmt ',
                         16, 1, 1, 16000, 32000, 2, 16, b'data', pcm_bytes)
    if (len(raw) != selected['bytes'] or raw[:44] != header
            or hashlib.sha256(raw).hexdigest() != request['sha256']):
        raise DispatchError('The reviewed audio failed its integrity check.')
    return raw, selected['frames'] / 16000


def uptime_ms():
    return int(time.clock_gettime(getattr(time, 'CLOCK_BOOTTIME', time.CLOCK_MONOTONIC)) * 1000)


def native_state(path, engine_uid, clock=uptime_ms):
    # This fixed privileged reader deliberately does not import dashboard code.
    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != engine_uid or parent.st_mode & 0o022:
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != engine_uid or info.st_mode & 0o022
                    or not 0 < info.st_size <= 4096):
                return None
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                value = json.loads(stream.read(4097), object_pairs_hook=unique_object)
        finally:
            os.close(descriptor)
        fields = {'schema', 'invocation_id', 'heartbeat_ms', 'online', 'rx_open', 'tx_on',
                  'control_busy', 'message_job', 'message_state', 'message_sequence'}
        if (not isinstance(value, dict) or set(value) != fields or type(value['schema']) is not int
                or value['schema'] != 1 or not isinstance(value['invocation_id'], str)
                or not TOKEN.fullmatch(value['invocation_id']) or type(value['heartbeat_ms']) is not int
                or not -1000 <= clock() - value['heartbeat_ms'] <= 4000
                or any(type(value[key]) is not bool for key in ('online', 'rx_open', 'tx_on', 'control_busy'))
                or not isinstance(value['message_job'], str)
                or value['message_job'] != '' and not TOKEN.fullmatch(value['message_job'])
                or not isinstance(value['message_state'], str) or value['message_state'] not in NATIVE_PHASES
                or type(value['message_sequence']) is not int or value['message_sequence'] < 0):
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def live_invocation(runner=subprocess.run):
    result = runner(['/usr/bin/systemctl', 'show', 'repeater-live.service', '--property=ActiveState',
                     '--property=InvocationID', '--property=MainPID'], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=3, check=False)
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key in fields:
            raise DispatchError('Live repeater status is unavailable.')
        fields[key] = value
    if (result.returncode or set(fields) != {'ActiveState', 'InvocationID', 'MainPID'}
            or fields['ActiveState'] != 'active' or not TOKEN.fullmatch(fields['InvocationID'])
            or not re.fullmatch(r'[1-9][0-9]*', fields['MainPID'])):
        raise DispatchError('Live repeater status is unavailable.')
    return fields['InvocationID']


def preflight(request):
    authorization = guard.check()
    state = guard.trusted_json(guard.STATE_PATH)
    if (authorization['catalog_digest'] != request['catalog_digest']
            or state['_pair']['pair_id'] != request['pair_id']
            or state['_pair']['catalog_digest'] != request['catalog_digest']):
        raise DispatchError('The reviewed channel pair changed.')
    return live_invocation()


def send_event(request, command_path, engine_uid):
    parent = command_path.parent.lstat()
    info = command_path.lstat()
    target = command_path.resolve(strict=True)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != engine_uid or parent.st_mode & 0o022
            or not stat.S_ISLNK(info.st_mode) or info.st_uid != engine_uid
            or re.fullmatch(r'/dev/pts/[0-9]+', str(target)) is None):
        raise DispatchError('The fixed live command interface is unavailable.')
    event = ('EVENT RepeaterLogic::box_message ' + request['invocation_id'] + ' '
             + request['request_id'] + '\n').encode('ascii')
    descriptor = os.open(target, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISCHR(info.st_mode) or info.st_uid != engine_uid
                or info.st_mode & 0o002 or not os.isatty(descriptor)):
            raise DispatchError('The fixed live command interface is unavailable.')
        if os.write(descriptor, event) != len(event):
            raise DispatchError('The control event was incomplete.')
    finally:
        os.close(descriptor)


def stop_live():
    result = subprocess.run(['/usr/bin/systemctl', 'stop', 'repeater-live.service'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=12, check=False)
    if result.returncode:
        raise DispatchError('The live repeater could not be stopped.')


class Dispatcher:
    def __init__(self, *, state_dir=STATE_DIR, messages_dir=MESSAGES_DIR, playback_dir=PLAYBACK_DIR,
                 consumed_dir=CONSUMED_DIR, native_path=NATIVE_PATH, command_path=COMMAND_PATH,
                 owner_uid=0, dashboard_uid=None, engine_uid=None, dashboard_gid=None, control_gid=None,
                 preflight_check=preflight, native_reader=None, event_sender=None, stop=stop_live,
                 clock=time.time, monotonic=time.monotonic, pause=time.sleep):
        self.state_dir, self.messages_dir = Path(state_dir), Path(messages_dir)
        self.playback_dir, self.consumed_dir = Path(playback_dir), Path(consumed_dir)
        self.owner_uid = owner_uid
        self.dashboard_uid = pwd.getpwnam('repeaterdash').pw_uid if dashboard_uid is None else dashboard_uid
        self.engine_uid = pwd.getpwnam('svxlink').pw_uid if engine_uid is None else engine_uid
        self.dashboard_gid = grp.getgrnam('repeaterdash').gr_gid if dashboard_gid is None else dashboard_gid
        self.control_gid = grp.getgrnam('repeater-control').gr_gid if control_gid is None else control_gid
        self.preflight = preflight_check
        self.native = native_reader or (lambda: native_state(Path(native_path), self.engine_uid))
        self.send = event_sender or (lambda request: send_event(request, Path(command_path), self.engine_uid))
        self.stop, self.clock, self.monotonic, self.pause = stop, clock, monotonic, pause

    def result(self, request, phase):
        value = {'schema': 1, 'request_id': request['request_id'], 'message_id': request['message_id'],
                 'phase': phase, 'success': True if phase == 'finished' else None if phase in PENDING else False,
                 'message': TEXT[phase], 'invocation_id': request['invocation_id'], 'sha256': request['sha256'],
                 'updated_at': datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        atomic_json(self.state_dir / RESULT_NAME, value, self.owner_uid, self.dashboard_gid, self.dashboard_uid)
        return value

    def reject_locked(self):
        """Reject lock contention immediately, without sending or stopping RF."""
        protected_directory(self.state_dir, self.dashboard_uid)
        request = read_json(self.state_dir / REQUEST_NAME, self.dashboard_uid, 4096, missing=True)
        if request is None:
            return None
        valid_request(request)
        previous = read_json(self.state_dir / RESULT_NAME, self.owner_uid, 8192, missing=True)
        if previous is not None:
            valid_result(previous)
            if previous['request_id'] == request['request_id']:
                if any(previous[key] != request[key] for key in ('message_id', 'invocation_id', 'sha256')):
                    raise DispatchError('A consumed request changed.')
                # Do not overwrite an in-flight owner's result if this helper
                # was invoked separately while that owner held the lock.
                return previous
            if previous['phase'] in PENDING:
                raise DispatchError('Another message dispatch is active.')
        return self.result(request, 'rejected')

    def consume(self, request):
        protected_directory(self.consumed_dir, self.owner_uid, mode=0o700)
        path = self.consumed_dir / 'consumed.json'
        data = read_json(path, self.owner_uid, 32768, missing=True)
        if data is None:
            data = {'schema': 1, 'requests': []}
        if (not isinstance(data, dict) or set(data) != {'schema', 'requests'}
                or type(data['schema']) is not int or data['schema'] != 1
                or not isinstance(data['requests'], list) or len(data['requests']) > 256):
            raise DispatchError('The consumed-message journal is invalid.')
        seen = set()
        for item in data['requests']:
            if (not isinstance(item, dict) or set(item) != {'request_id', 'consumed_at'}
                    or not isinstance(item['request_id'], str) or not TOKEN.fullmatch(item['request_id'])
                    or item['request_id'] in seen or type(item['consumed_at']) is not int
                    or item['consumed_at'] < 0):
                raise DispatchError('The consumed-message journal is invalid.')
            seen.add(item['request_id'])
        if request['request_id'] in seen:
            return False
        now = int(self.clock())
        entries = data['requests']
        if len(entries) >= 256:
            entries = [item for item in entries if now - item['consumed_at'] <= 60]
        if len(entries) >= 256:
            raise DispatchError('The consumed-message journal is full.')
        entries.append({'request_id': request['request_id'], 'consumed_at': now})
        atomic_json(path, {'schema': 1, 'requests': entries}, self.owner_uid, self.control_gid,
                    self.owner_uid, mode=0o600)
        return True

    def snapshot(self, request, raw):
        protected_directory(self.playback_dir, self.owner_uid, mode=0o750)
        path = self.playback_dir / (request['request_id'] + '.wav')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
        try:
            os.fchmod(descriptor, 0o640)
            os.fchown(descriptor, self.owner_uid, self.control_gid)
            with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            sync_directory(self.playback_dir)
        finally:
            os.close(descriptor)
        return path

    def idle(self, request):
        native = self.native()
        if (native is None or native['invocation_id'] != request['invocation_id'] or not native['online']
                or native['rx_open'] or native['tx_on'] or native['control_busy']
                or native['message_state'] in ('accepted', 'playing')):
            raise DispatchError('The live repeater is unavailable or busy.')
        return native['message_sequence']

    def poll(self, request, duration, baseline, deadline):
        phase, playing_at, dispatched_at = 'preparing', None, self.monotonic()
        sequence = baseline
        while self.monotonic() < deadline:
            native = self.native()
            if native is None or native['invocation_id'] != request['invocation_id'] or not native['online']:
                raise DispatchError('Live playback state became unavailable.')
            if native['message_sequence'] < sequence:
                raise DispatchError('Live playback state moved backward.')
            if native['message_job'] == request['request_id']:
                current = native['message_state']
                if native['message_sequence'] <= baseline:
                    raise DispatchError('Live playback acknowledgment is invalid.')
                sequence = native['message_sequence']
                if current == 'accepted':
                    if phase not in ('preparing', 'accepted') or sequence != baseline + 1:
                        raise DispatchError('Live playback moved backward.')
                    if phase != 'accepted':
                        self.result(request, 'accepted'); phase = 'accepted'
                elif current == 'playing':
                    if not native['tx_on'] or sequence != baseline + 2:
                        raise DispatchError('Native playback has no transmit state.')
                    if playing_at is None:
                        playing_at = self.monotonic()
                        self.result(request, 'playing'); phase = 'playing'
                elif current == 'finished':
                    # A very short clip can complete between polls. The fixed
                    # native counter increments once for each accepted, playing
                    # and finished transition, proving the missed playing step.
                    if (sequence != baseline + 3 or native['tx_on']
                            or self.monotonic() - dispatched_at < max(0, duration - 0.25)
                            or playing_at is not None
                            and self.monotonic() - playing_at < max(0, duration - 0.5)):
                        raise DispatchError('Native playback ended before its reviewed duration.')
                    return 'finished'
                elif current == 'rejected_busy':
                    if playing_at is not None:
                        raise DispatchError('Live playback changed after transmission began.')
                    return 'rejected'
                else:
                    raise DispatchError('Native playback failed.')
            elif phase != 'preparing':
                raise DispatchError('The active native message changed.')
            self.pause(min(0.05, max(0, deadline - self.monotonic())))
        raise DispatchError('Native playback exceeded its time limit.')

    def run(self):
        protected_directory(self.state_dir, self.dashboard_uid)
        request = read_json(self.state_dir / REQUEST_NAME, self.dashboard_uid, 4096, missing=True)
        if request is None:
            return None
        valid_request(request)
        previous = read_json(self.state_dir / RESULT_NAME, self.owner_uid, 8192, missing=True)
        if previous is not None:
            valid_result(previous)
            if previous['request_id'] == request['request_id']:
                if (previous['message_id'] != request['message_id'] or previous['invocation_id'] != request['invocation_id']
                        or previous['sha256'] != request['sha256']):
                    raise DispatchError('A consumed request changed.')
                if previous['phase'] in PENDING:
                    try:
                        self.stop()
                    finally:
                        return self.result(request, 'failed')
                return previous
        snapshot, attempted = None, False
        deadline = self.monotonic() + 40
        try:
            age = self.clock() - timestamp(request['requested_at'])
            if not -5 <= age <= 30:
                return self.result(request, 'cancelled')
            if self.preflight(request) != request['invocation_id']:
                raise DispatchError('The live repeater invocation changed.')
            baseline = self.idle(request)
            raw, duration = canonical_asset(request, self.messages_dir, self.dashboard_uid)
            snapshot = self.snapshot(request, raw)
            # Recheck after copying, before irreversibly consuming and sending.
            if (read_json(self.state_dir / REQUEST_NAME, self.dashboard_uid, 4096) != request
                    or self.preflight(request) != request['invocation_id']):
                raise DispatchError('The reviewed request or live state changed.')
            baseline = self.idle(request)
            if not -5 <= self.clock() - timestamp(request['requested_at']) <= 30:
                return self.result(request, 'cancelled')
            if deadline - self.monotonic() < duration + 2:
                raise DispatchError('Insufficient time remains for the complete reviewed message.')
            if not self.consume(request):
                return self.result(request, 'cancelled')
            self.result(request, 'preparing')
            # Durable spool writes can be slow too. Never begin a clip that the
            # fixed watchdog would necessarily cut short, or dispatch a stale
            # approval after those writes complete.
            if not -5 <= self.clock() - timestamp(request['requested_at']) <= 30:
                return self.result(request, 'cancelled')
            if deadline - self.monotonic() < duration + 2:
                raise DispatchError('Insufficient time remains for the complete reviewed message.')
            baseline = self.idle(request)
            attempted = True  # A partial write is also an uncertain dispatch.
            self.send(request)
            phase = self.poll(request, duration, baseline, deadline)
            return self.result(request, phase)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
            if attempted:
                try:
                    self.stop()
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            return self.result(request, 'failed' if attempted else 'rejected')
        finally:
            if snapshot is not None:
                try:
                    snapshot.unlink()
                    sync_directory(self.playback_dir)
                except FileNotFoundError:
                    pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv or os.geteuid() != 0:
        return 1
    descriptor = None
    try:
        descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise DispatchError('The maintenance lock is not protected.')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            Dispatcher().reject_locked()
            return 0
        Dispatcher().run()
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
        print('The message request could not be safely processed.', file=sys.stderr)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == '__main__':
    raise SystemExit(main())
