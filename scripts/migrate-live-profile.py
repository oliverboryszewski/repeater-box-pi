#!/usr/bin/python3
"""Upgrade a trusted private live profile without granting new RF authorization."""
import fcntl
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys

SPEC = importlib.util.spec_from_file_location('configure_live_migration', Path(__file__).with_name('configure-live.py'))
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)
guard = configure.guard


def _protected_parent(path, owner_uid):
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise ValueError('The local configuration directory must be protected and root-owned.')


def _exists(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _leave_disabled(auth_path, authorization, group_id, owner_uid):
    """A failed replacement must never leave an enabled authorization behind."""
    disabled = dict(authorization, enabled=False)
    try:
        configure.atomic_write(auth_path, configure.json_bytes(disabled), group_id, owner_uid)
    except BaseException:
        # If the filesystem cannot replace the revocation, removing authorization
        # is the remaining fail-closed option. Its absence blocks normal startup.
        try:
            auth_path.unlink()
        except FileNotFoundError:
            pass
        directory_fd = os.open(auth_path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def migrate(config_path=guard.CONFIG_PATH, auth_path=guard.AUTH_PATH, owner_uid=0, writer=None):
    """Migrate static files under the caller's maintenance lock; open no hardware.

    Return False for a fresh installation or a valid current profile. Radio-job
    state is deliberately not read: the normal startup guard still checks it.
    """
    config_path, auth_path = Path(config_path), Path(auth_path)
    present = (_exists(config_path), _exists(auth_path))
    if present == (False, False):
        return False
    if present != (True, True) or config_path == auth_path:
        raise ValueError('The private live profile and authorization must both be present.')
    _protected_parent(config_path, owner_uid)
    _protected_parent(auth_path, owner_uid)
    content = guard.trusted_bytes(config_path, owner_uid, 16384)
    authorization = guard.validate_authorization(
        guard.trusted_json(auth_path, owner_uid, 16384), require_enabled=False)
    if hashlib.sha256(content).hexdigest() != authorization['config_sha256']:
        raise ValueError('The private live configuration changed outside its authorization.')
    parser = guard.parser_from_bytes(content)
    try:
        guard.validate_parser(parser, authorization)
    except ValueError:
        guard._validate_legacy_parser(parser, authorization)
    else:
        return False

    # The complete old profile has been checked before any managed file changes.
    parser['RepeaterLogic']['TX'] = 'TxMonitored'
    parser['TxAIOC']['TIMEOUT'] = '40'
    parser['TxMonitored'] = guard.TX_COMPOSITE
    parser['TxMonitor'] = guard.TX_MONITOR
    guard.validate_parser(parser, authorization)
    output = io.StringIO()
    parser.write(output, space_around_delimiters=False)
    updated_content = output.getvalue().encode('utf-8')
    updated_authorization = dict(authorization, config_sha256=hashlib.sha256(updated_content).hexdigest())
    disabled = dict(authorization, enabled=False)
    auth_group_id = auth_path.lstat().st_gid
    engine_group_id = config_path.lstat().st_gid
    write = configure.atomic_write if writer is None else writer
    try:
        write(auth_path, configure.json_bytes(disabled), auth_group_id, owner_uid)
        write(config_path, updated_content, engine_group_id, owner_uid)
        # Preserve the preexisting flag only after the new profile is durable.
        write(auth_path, configure.json_bytes(updated_authorization), auth_group_id, owner_uid)
    except BaseException:
        _leave_disabled(auth_path, authorization, auth_group_id, owner_uid)
        raise
    return True


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv or os.geteuid() != 0:
        print('Run this fixed local migration with sudo and no arguments.', file=sys.stderr)
        return 1
    descriptor = None
    try:
        descriptor = os.open(configure.LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('The local maintenance lock is not protected.')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configure.live_stopped()
        changed = migrate()
        print('Private live audio profile migrated.' if changed else 'Private live audio profile needs no migration.')
        return 0
    except BlockingIOError:
        print('A maintenance operation is running. Retry after it finishes.', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, guard.configparser.Error) as error:
        print('Private live profile was not migrated: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == '__main__':
    raise SystemExit(main())
