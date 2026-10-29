"""Publish one saved station-ID preference; never open radio hardware."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

PROJECTION_PATH = Path('/run/repeater-box-control/station-identification.json')
SETTING = 'automatic_station_id'
MAXIMUM_BYTES = 64


def payload(enabled):
    if type(enabled) is not bool:
        raise ValueError('enabled must be true or false')
    return (json.dumps({'schema': 1, 'enabled': enabled}, separators=(',', ':')) + '\n').encode('ascii')


class StationIdentification:
    """Caller serializes mutations with the dashboard's settings/mutation locks.

    The projection contains only a boolean preference. Its availability is not
    evidence of transmitter activity, identification delivery, or reception.
    """
    def __init__(self, settings, projection_path=PROJECTION_PATH, owner_uid=None):
        self.settings = settings
        self.path = Path(projection_path) if projection_path is not None else None
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self.lock = threading.RLock()
        self.available = False
        if self.path is not None:
            try:
                self.publish()
            except (OSError, ValueError, TypeError):
                # A missing runtime directory must not take down the dashboard.
                # The native policy retains its last known preference meanwhile.
                self.available = False

    def _saved_enabled(self):
        value = self.settings.get().get(SETTING, True)
        if type(value) is not bool:
            raise ValueError('The saved automatic station identification preference is invalid')
        return value

    def _checked_parent(self):
        if self.path is None:
            raise ValueError('Automatic station identification changes are unavailable in this preview')
        info = self.path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022):
            raise ValueError('The station identification runtime directory is not protected')

    def _read_projection(self):
        self._checked_parent()
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                    or info.st_mode & 0o022 or info.st_size > MAXIMUM_BYTES):
                raise ValueError('The station identification projection is not protected')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                content = stream.read(MAXIMUM_BYTES + 1)
            if len(content) > MAXIMUM_BYTES:
                raise ValueError('The station identification projection is too large')
            return content, stat.S_IMODE(info.st_mode)
        finally:
            os.close(descriptor)

    def _write_projection(self, enabled):
        content = payload(enabled)
        self._checked_parent()
        try:
            self._read_projection()
        except FileNotFoundError:
            pass
        descriptor, temporary = tempfile.mkstemp(prefix='.station-identification-', dir=self.path.parent)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != self.owner_uid:
                raise ValueError('The station identification projection owner is invalid')
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, 'wb') as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def status(self):
        with self.lock:
            try:
                enabled = self._saved_enabled()
            except (ValueError, TypeError):
                return {'available': False, 'enabled': True}
            available = False
            if self.path is not None and self.available:
                try:
                    content, mode = self._read_projection()
                    available = content == payload(enabled) and mode == 0o644
                except (OSError, ValueError, TypeError):
                    pass
            return {'available': available, 'enabled': enabled}

    def publish(self):
        with self.lock:
            if self.path is None:
                self.available = False
                return self.status()
            self.available = False
            self._write_projection(self._saved_enabled())
            self.available = True
            return self.status()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be true or false')
        with self.lock:
            if self.path is None:
                raise ValueError('Automatic station identification changes are unavailable in this preview')
            previous = self._saved_enabled()
            try:
                self.settings.update({SETTING: enabled})
                self._write_projection(enabled)
            except (OSError, ValueError, TypeError):
                self.available = False
                try:
                    self.settings.update({SETTING: previous})
                    # Atomic replace may succeed before directory fsync fails.
                    # Restore the previous projection as well when possible.
                    self._write_projection(previous)
                    self.available = True
                except (OSError, ValueError, TypeError):
                    pass
                raise ValueError('The automatic station identification change could not be applied; check dashboard storage and retry') from None
            self.available = True
            return self.status()
