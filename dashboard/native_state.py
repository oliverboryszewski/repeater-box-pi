"""Bounded, read-only view of the live engine's non-secret playback state."""
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time

STATE_PATH = Path('/run/repeater-message-status/state.json')
TOKEN = re.compile(r'[0-9a-f]{32}\Z')
PHASES = frozenset(('idle', 'accepted', 'playing', 'finished', 'rejected_busy', 'failed'))
FIELDS = frozenset(('schema', 'invocation_id', 'heartbeat_ms', 'online', 'rx_open',
                    'tx_on', 'control_busy', 'message_job', 'message_state', 'message_sequence'))


def uptime_ms():
    clock = getattr(time, 'CLOCK_BOOTTIME', time.CLOCK_MONOTONIC)
    return int(time.clock_gettime(clock) * 1000)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate native state field')
        result[key] = value
    return result


class NativeState:
    def __init__(self, path=STATE_PATH, *, owner_uid=None, clock=uptime_ms):
        self.path = Path(path)
        self.clock = clock
        if owner_uid is None:
            try:
                owner_uid = pwd.getpwnam('svxlink').pw_uid
            except KeyError:
                owner_uid = -1
        self.owner_uid = owner_uid

    def read(self):
        """Reject stale/malformed state; never infer physical RF reception."""
        descriptor = None
        try:
            parent = self.path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != self.owner_uid
                    or parent.st_mode & 0o022):
                return None
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                    or info.st_mode & 0o022 or not 0 < info.st_size <= 4096):
                return None
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return None
            value = json.loads(raw.decode('ascii'), object_pairs_hook=unique_object)
            if (not isinstance(value, dict) or set(value) != FIELDS
                    or type(value['schema']) is not int or value['schema'] != 1
                    or not isinstance(value['invocation_id'], str) or not TOKEN.fullmatch(value['invocation_id'])
                    or type(value['heartbeat_ms']) is not int
                    or any(type(value[key]) is not bool for key in ('online', 'rx_open', 'tx_on', 'control_busy'))
                    or not isinstance(value['message_job'], str)
                    or value['message_job'] != '' and not TOKEN.fullmatch(value['message_job'])
                    or value['message_state'] not in PHASES
                    or type(value['message_sequence']) is not int or value['message_sequence'] < 0
                    or not -1000 <= self.clock() - value['heartbeat_ms'] <= 4000):
                return None
            return value
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def live(self):
        value = self.read()
        return bool(value and value['online'])
