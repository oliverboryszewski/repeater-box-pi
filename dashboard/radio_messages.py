"""Private bounded audio-message library and explicit one-shot request spool.

This module never opens radio devices, command PTYs or sockets and never starts
services. The separately installed root dispatcher owns transmission decisions.
"""
import datetime as dt
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time
import wave

MESSAGES_DIR = Path('/var/lib/repeater-messages')
CONVERTER = Path('/opt/repeater-box/scripts/convert-message-audio.py')
TOKEN_RE = re.compile(r'[0-9a-f]{32}\Z')
HASH_RE = re.compile(r'[0-9a-f]{64}\Z')
PAIR_RE = re.compile(r'[a-z][a-z0-9_-]{0,15}\Z')
DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z')
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 30 * 16000
MAX_MESSAGES = 10
MAX_DECODED_BYTES = 2 * 1024 * 1024
CONVERSION_TIMEOUT = 20
PHASES = frozenset(('preparing', 'accepted', 'playing', 'finished', 'rejected', 'failed', 'cancelled'))
PENDING_PHASES = frozenset(('preparing', 'accepted', 'playing'))
NATIVE_STATES = frozenset(('idle', 'accepted', 'playing', 'finished', 'rejected_busy', 'failed'))
PHASE_TEXT = {
    'preparing': 'Checking the reviewed message and live repeater.',
    'accepted': 'Message accepted by the live repeater.',
    'playing': 'Message playback is active.',
    'finished': 'Message playback finished.',
    'rejected': 'The live repeater could not accept this message. Review its current state before trying again.',
    'failed': 'Message playback could not be confirmed. Review live repeater status before trying again.',
    'cancelled': 'Message request cancelled.',
}


class MessageError(ValueError):
    """A fixed operational message safe to display in the local dashboard."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read_json(path, maximum, owner_uid):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o027
                or not 0 < info.st_size <= maximum):
            raise MessageError('The message library needs a local integrity check.')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise MessageError('The message library needs a local integrity check.')
        return json.loads(data, object_pairs_hook=unique_object)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        raise MessageError('The message library needs a local integrity check.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.message-state-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            descriptor = None
            json.dump(value, stream, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def upload_name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or len(value.encode('utf-8')) > 256 or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or '/' in value or '\\' in value):
        raise MessageError('Choose a WAV, MP3 or M4A filename without folders.')
    extension = value.rsplit('.', 1)[-1].lower() if '.' in value else ''
    if extension not in ('wav', 'mp3', 'm4a'):
        raise MessageError('Choose a WAV, MP3 or M4A audio file.')
    return value, extension


def run_conversion(source, destination, extension):
    try:
        result = subprocess.run(['/usr/bin/python3', str(CONVERTER), extension, str(source), str(destination)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=CONVERSION_TIMEOUT, start_new_session=True, check=False)
    except subprocess.TimeoutExpired:
        raise MessageError('Audio conversion timed out. Try another audio file.') from None
    except OSError:
        raise MessageError('Audio conversion tools are unavailable on this Pi.') from None
    if result.returncode != 0:
        raise MessageError('Audio conversion failed. Try another WAV, MP3 or M4A file.')


def pcm_audio(path):
    """Read only a small regular decoded file; no metadata reaches the radio."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o027
                or not 0 < info.st_size <= MAX_DECODED_BYTES):
            raise MessageError('The decoded audio is invalid.')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream, wave.open(stream, 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise MessageError('The decoded audio is invalid.')
            frames = audio.getnframes()
            if not 0 < frames <= MAX_FRAMES:
                raise MessageError('Messages must contain audio and be no longer than 30 seconds.')
            pcm = audio.readframes(frames + 1)
            if len(pcm) != frames * 2:
                raise MessageError('The decoded audio is incomplete.')
            return pcm
    except MessageError:
        raise
    except (OSError, EOFError, wave.Error, ValueError):
        raise MessageError('The decoded audio is invalid.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class RadioMessages:
    REQUEST = 'message-broadcast-request.json'
    RESULT = 'message-broadcast-result.json'

    def __init__(self, state_dir, messages_dir=MESSAGES_DIR, native_state_reader=None,
                 busy_check=None, converter=None, clock=time.time, result_owner_uid=0, mutation_lock=None):
        self.state_dir, self.messages_dir = Path(state_dir), Path(messages_dir)
        self.native_state_reader, self.busy_check = native_state_reader, busy_check
        self.converter, self.clock = converter or run_conversion, clock
        self.converter_supplied = converter is not None
        self.result_owner_uid = result_owner_uid
        self.mutation_lock = mutation_lock
        self.lock = threading.RLock()
        self.upload_pending = False
        try:
            self._directory(self.messages_dir)
            self._cleanup_stale_uploads()
        except (OSError, MessageError):
            # Status reports an unavailable or damaged installation; startup
            # never overwrites that condition or dispatches an old request.
            pass

    @property
    def active(self):
        """Maintenance exclusion without invoking cross-controller callbacks."""
        with self.lock:
            try:
                return self.upload_pending or bool((self._job() or {}).get('pending'))
            except (OSError, MessageError):
                return True

    def _directory(self, path, create=False):
        try:
            if create:
                path.mkdir(mode=0o750, parents=True, exist_ok=True)
            info = path.lstat()
        except OSError:
            raise MessageError('The message library needs a local integrity check.') from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o027:
            raise MessageError('The message library needs a local integrity check.')

    def _stored_count(self):
        # A power failure between publishing audio and indexing it may leave a
        # harmless orphan. Count it too so storage remains bounded at ten files.
        return sum(1 for path in self.messages_dir.glob('*.wav') if TOKEN_RE.fullmatch(path.stem))

    def _cleanup_stale_uploads(self):
        for folder in self.messages_dir.glob('.upload-*'):
            if not re.fullmatch(r'\.upload-[a-z0-9_]{8}', folder.name):
                continue
            info = folder.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or time.time() - info.st_mtime < 60):
                continue
            children = list(folder.iterdir())
            if any(path.name not in ('source', 'decoded.wav', 'canonical.wav')
                   or not (stat.S_ISREG(path.lstat().st_mode) or path.is_symlink()) for path in children):
                continue
            # Unlink only known files without following a symlink. A decoder
            # has a 20-second lifetime, so these old private workspaces are idle.
            for path in children:
                path.unlink()
            folder.rmdir()

    def _entries(self):
        self._directory(self.messages_dir)
        data = read_json(self.messages_dir / 'index.json', 32768, os.geteuid())
        if data is None:
            return []
        if (not isinstance(data, dict) or set(data) != {'schema', 'messages'}
                or type(data['schema']) is not int or data['schema'] != 1
                or not isinstance(data['messages'], list) or len(data['messages']) > MAX_MESSAGES):
            raise MessageError('The message library needs a local integrity check.')
        seen = set()
        for item in data['messages']:
            if (not isinstance(item, dict) or set(item) != {'id', 'sha256', 'name', 'frames', 'bytes', 'created_at'}
                    or not isinstance(item['id'], str) or not TOKEN_RE.fullmatch(item['id']) or item['id'] in seen
                    or not isinstance(item['sha256'], str) or not HASH_RE.fullmatch(item['sha256'])
                    or type(item['frames']) is not int or not 0 < item['frames'] <= MAX_FRAMES
                    or type(item['bytes']) is not int or item['bytes'] != 44 + item['frames'] * 2
                    or not isinstance(item['created_at'], str) or not DATE_RE.fullmatch(item['created_at'])):
                raise MessageError('The message library needs a local integrity check.')
            upload_name(item['name'])
            seen.add(item['id'])
            try:
                info = (self.messages_dir / (item['id'] + '.wav')).lstat()
            except OSError:
                raise MessageError('The message library needs a local integrity check.') from None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o027 or info.st_size != item['bytes']):
                raise MessageError('The message library needs a local integrity check.')
        return data['messages']

    @staticmethod
    def _public(item):
        return {key: item[key] for key in ('id', 'sha256', 'name', 'bytes', 'created_at')} | {
            'duration_seconds': item['frames'] / 16000,
            'preview_url': '/api/messages/' + item['id'] + '/audio'}

    def _busy_reason(self):
        if self.busy_check is None:
            return None
        try:
            reason = self.busy_check()
            if reason is None:
                return None
            if isinstance(reason, str) and 0 < len(reason) <= 200 and not any(ord(c) < 32 for c in reason):
                return reason
        except Exception:
            pass
        return 'Current maintenance and radio state could not be confirmed.'

    def _native(self):
        if self.native_state_reader is None:
            return None
        try:
            value = self.native_state_reader()
        except Exception:
            return None
        if (not isinstance(value, dict) or set(value) != {'schema', 'invocation_id', 'heartbeat_ms',
                'online', 'rx_open', 'tx_on', 'control_busy', 'message_job', 'message_state', 'message_sequence'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['invocation_id'], str) or not TOKEN_RE.fullmatch(value['invocation_id'])
                or any(type(value[key]) is not bool for key in ('online', 'rx_open', 'tx_on', 'control_busy'))
                or any(type(value[key]) is not int or value[key] < 0 for key in ('heartbeat_ms', 'message_sequence'))
                or not isinstance(value['message_job'], str)
                or value['message_job'] != '' and not TOKEN_RE.fullmatch(value['message_job'])
                or not isinstance(value['message_state'], str) or value['message_state'] not in NATIVE_STATES):
            return None
        return value

    def _job(self):
        request = read_json(self.state_dir / self.REQUEST, 4096, os.geteuid())
        if request is None:
            return None
        required = {'schema', 'request_id', 'action', 'message_id', 'sha256', 'pair_id',
                    'catalog_digest', 'invocation_id', 'requested_at'}
        if (not isinstance(request, dict) or set(request) != required
                or type(request['schema']) is not int or request['schema'] != 1 or request['action'] != 'broadcast'
                or any(not isinstance(request[key], str) or not TOKEN_RE.fullmatch(request[key])
                       for key in ('request_id', 'message_id', 'invocation_id'))
                or any(not isinstance(request[key], str) or not HASH_RE.fullmatch(request[key])
                       for key in ('sha256', 'catalog_digest'))
                or not isinstance(request['pair_id'], str) or not PAIR_RE.fullmatch(request['pair_id'])
                or not isinstance(request['requested_at'], str) or not DATE_RE.fullmatch(request['requested_at'])):
            raise MessageError('An unfinished message request needs a local integrity check.')
        result = read_json(self.state_dir / self.RESULT, 8192, self.result_owner_uid)
        phase = 'preparing'
        if isinstance(result, dict) and result.get('request_id') == request['request_id']:
            allowed = {'schema', 'request_id', 'message_id', 'phase', 'success', 'message',
                       'invocation_id', 'sha256', 'updated_at'}
            if (not {'schema', 'request_id', 'message_id', 'phase'} <= set(result) <= allowed
                    or type(result['schema']) is not int or result['schema'] != 1
                    or result['message_id'] != request['message_id']
                    or not isinstance(result['phase'], str) or result['phase'] not in PHASES
                    or 'success' in result and result['success'] is not None and type(result['success']) is not bool
                    or 'message' in result and (not isinstance(result['message'], str) or len(result['message']) > 512)
                    or 'invocation_id' in result and result['invocation_id'] != request['invocation_id']
                    or 'sha256' in result and result['sha256'] != request['sha256']
                    or 'updated_at' in result and (not isinstance(result['updated_at'], str) or len(result['updated_at']) > 40)):
                raise MessageError('An unfinished message request needs a local integrity check.')
            phase = result['phase']
            expected_success = True if phase == 'finished' else None if phase in PENDING_PHASES else False
            if 'success' in result and result['success'] is not expected_success:
                raise MessageError('An unfinished message request needs a local integrity check.')
        elif result is not None and not isinstance(result, dict):
            raise MessageError('An unfinished message request needs a local integrity check.')
        return {'request_id': request['request_id'], 'message_id': request['message_id'],
                'phase': phase, 'pending': phase in PENDING_PHASES,
                'success': True if phase == 'finished' else False if phase not in PENDING_PHASES else None,
                'message': PHASE_TEXT[phase], 'requested_at': request['requested_at']}

    def status(self):
        busy, native = self._busy_reason(), self._native()
        limits = {'max_upload_bytes': MAX_UPLOAD_BYTES, 'max_duration_seconds': 30,
                  'allowed_extensions': ['wav', 'mp3', 'm4a'], 'max_messages': MAX_MESSAGES}
        with self.lock:
            try:
                entries, job = self._entries(), self._job()
            except (OSError, MessageError):
                return {'available': False, 'can_upload': False, 'can_broadcast': False,
                    'reason': 'The message library needs a local integrity check.', 'limits': limits,
                    'messages': [], 'upload_pending': self.upload_pending, 'job': None,
                    'invocation_id': native['invocation_id'] if native else None}
            pending = self.upload_pending or bool(job and job['pending'])
            tools = self.converter_supplied or (CONVERTER.is_file() and os.access('/usr/bin/ffmpeg', os.X_OK))
            upload_reason = (busy or 'Wait for the current message task.' if pending else busy)
            if not tools:
                upload_reason = upload_reason or 'Audio conversion tools are not installed.'
            if self._stored_count() >= MAX_MESSAGES:
                upload_reason = upload_reason or 'Delete a stored message before uploading another.'
            broadcast_reason = busy or ('Wait for the current message task.' if pending else None)
            if not entries:
                broadcast_reason = broadcast_reason or 'Upload and preview a message first.'
            if native is None or not native['online']:
                broadcast_reason = broadcast_reason or 'A current live repeater status is required to broadcast.'
            elif (native['rx_open'] or native['tx_on'] or native['control_busy']
                  or native['message_state'] in ('accepted', 'playing')):
                broadcast_reason = broadcast_reason or 'The radio channel or control menu is busy.'
            return {'available': True, 'can_upload': upload_reason is None,
                'can_broadcast': broadcast_reason is None,
                'reason': broadcast_reason or upload_reason or '', 'limits': limits,
                'messages': [self._public(item) for item in entries],
                'upload_pending': self.upload_pending, 'job': job,
                'invocation_id': native['invocation_id'] if native else None}

    def _require_idle(self):
        busy = self._busy_reason()
        if busy:
            raise MessageError(busy)
        if self.upload_pending or bool((self._job() or {}).get('pending')):
            raise MessageError('Wait for the current message task.')

    def store_upload(self, name, payload):
        name, extension = upload_name(name)
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_UPLOAD_BYTES:
            raise MessageError('Upload an audio file no larger than 8 MiB.')
        busy = self._busy_reason()
        if busy:
            raise MessageError(busy)
        with self.mutation_lock if self.mutation_lock is not None else nullcontext():
            with self.lock:
                self._directory(self.messages_dir, create=True)
                self._directory(self.state_dir, create=True)
                self._require_idle()
                self._cleanup_stale_uploads()
                self._entries()
                if self._stored_count() >= MAX_MESSAGES:
                    raise MessageError('Delete a stored message before uploading another.')
                self.upload_pending = True
        try:
            # Only the reservation is locked. Decode in a fresh limited process,
            # leaving status/DTMF polling free to run while this request waits.
            with tempfile.TemporaryDirectory(prefix='.upload-', dir=self.messages_dir) as temporary:
                folder = Path(temporary)
                folder.chmod(0o700)
                source, decoded, canonical = (folder / name for name in ('source', 'decoded.wav', 'canonical.wav'))
                descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(payload)
                self.converter(source, decoded, extension)
                pcm = pcm_audio(decoded)
                with wave.open(str(canonical), 'wb') as audio:
                    audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                    audio.writeframes(pcm)
                os.chown(canonical, -1, self.messages_dir.stat().st_gid)
                os.chmod(canonical, 0o640)
                with canonical.open('rb') as stream:
                    os.fsync(stream.fileno())
                raw = canonical.read_bytes()
                identifier = secrets.token_hex(16)
                item = {'id': identifier, 'sha256': hashlib.sha256(raw).hexdigest(), 'name': name,
                    'frames': len(pcm) // 2, 'bytes': len(raw),
                    'created_at': dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
                with self.mutation_lock if self.mutation_lock is not None else nullcontext():
                    with self.lock:
                        busy = self._busy_reason()
                        if busy:
                            raise MessageError(busy)
                        entries = self._entries()
                        if self._stored_count() >= MAX_MESSAGES:
                            raise MessageError('Delete a stored message before uploading another.')
                        final = self.messages_dir / (identifier + '.wav')
                        os.link(canonical, final, follow_symlinks=False)
                        # Keep an immutable orphan if metadata persistence is
                        # uncertain; _stored_count still charges its quota.
                        atomic_json(self.messages_dir / 'index.json', {'schema': 1, 'messages': entries + [item]})
                return self._public(item)
        except MessageError:
            raise
        except subprocess.TimeoutExpired:
            raise MessageError('Audio conversion timed out. Try another audio file.') from None
        except Exception:
            raise MessageError('The audio message could not be saved. Try another audio file.') from None
        finally:
            with self.mutation_lock if self.mutation_lock is not None else nullcontext():
                with self.lock:
                    self.upload_pending = False

    def _message(self, identifier, digest=None):
        if not isinstance(identifier, str) or not TOKEN_RE.fullmatch(identifier):
            raise MessageError('Choose a stored audio message.')
        if digest is not None and (not isinstance(digest, str) or not HASH_RE.fullmatch(digest)):
            raise MessageError('Review the stored audio message again.')
        item = next((entry for entry in self._entries() if entry['id'] == identifier), None)
        if item is None:
            raise MessageError('This stored audio message is no longer available.')
        if digest is not None and digest != item['sha256']:
            raise MessageError('The reviewed audio message changed. Preview it again.')
        return item

    def open_preview(self, identifier):
        """Return (binary stream, public metadata); the HTTP caller closes it."""
        with self.lock:
            item = self._message(identifier)
            descriptor = None
            stream = None
            returned = False
            try:
                descriptor = os.open(self.messages_dir / (identifier + '.wav'),
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o027 or info.st_size != item['bytes']):
                    raise MessageError('The stored message failed its integrity check.')
                stream = os.fdopen(descriptor, 'rb')
                descriptor = None
                raw = stream.read(item['bytes'] + 1)
                if len(raw) != item['bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                    stream.close()
                    raise MessageError('The stored message failed its integrity check.')
                stream.seek(0)
                returned = True
                return stream, self._public(item)
            except OSError:
                raise MessageError('The stored message failed its integrity check.') from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if stream is not None and not returned:
                    stream.close()

    def delete_message(self, identifier, digest):
        self._require_idle()
        with self.lock:
            self._require_idle()
            item = self._message(identifier, digest)
            entries = [entry for entry in self._entries() if entry['id'] != identifier]
            # Updating metadata first can leave only a harmless orphan after a
            # power failure; no request ever selects unindexed audio.
            try:
                atomic_json(self.messages_dir / 'index.json', {'schema': 1, 'messages': entries})
                (self.messages_dir / (item['id'] + '.wav')).unlink()
                sync_directory(self.messages_dir)
            except OSError:
                raise MessageError('The stored message could not be deleted. Refresh the message list.') from None
        return self.status()

    def request_broadcast(self, identifier, digest, pair_id, catalog_digest, invocation_id):
        self._require_idle()
        if (not isinstance(pair_id, str) or not PAIR_RE.fullmatch(pair_id)
                or not isinstance(catalog_digest, str) or not HASH_RE.fullmatch(catalog_digest)
                or not isinstance(invocation_id, str) or not TOKEN_RE.fullmatch(invocation_id)):
            raise MessageError('Review the current channel pair and live repeater before broadcasting.')
        native = self._native()
        if native is None or native['invocation_id'] != invocation_id or not native['online']:
            raise MessageError('Live repeater status changed. Review the broadcast again.')
        if (native['rx_open'] or native['tx_on'] or native['control_busy']
                or native['message_state'] in ('accepted', 'playing')):
            raise MessageError('The radio channel or control menu is busy.')
        with self.lock:
            self._require_idle()
            item = self._message(identifier, digest)
            stream, _metadata = self.open_preview(identifier)
            stream.close()
            self._directory(self.state_dir, create=True)
            request = {'schema': 1, 'request_id': secrets.token_hex(16), 'action': 'broadcast',
                'message_id': item['id'], 'sha256': item['sha256'], 'pair_id': pair_id,
                'catalog_digest': catalog_digest, 'invocation_id': invocation_id,
                'requested_at': dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
            atomic_json(self.state_dir / self.REQUEST, request)
        return self.status()
