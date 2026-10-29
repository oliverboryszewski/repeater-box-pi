#!/usr/bin/python3
"""Prepare and authorize a private local repeater; never start RF automatically."""
import argparse
import fcntl
import grp
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

SPEC = importlib.util.spec_from_file_location('live_guard', Path(__file__).with_name('check-live-config.py'))
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
LOCK_PATH = Path('/opt/repeater-box/.maintenance.lock')
MONITOR_PATH = Path('/etc/repeater-box/svxlink-rx-monitor.conf')


def live_stopped(runner=subprocess.run):
    result = runner(['/usr/bin/systemctl', 'show', 'repeater-live.service', '--property=ActiveState', '--value'],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
    if result.returncode or result.stdout.strip() not in ('inactive', 'failed'):
        raise ValueError('Stop repeater-live.service before preparing or changing its authorization.')


def atomic_write(path, content, group_id, owner_uid=0):
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner_uid or directory.st_mode & 0o022:
        raise ValueError('The local configuration directory must be protected and root-owned.')
    try:
        guard.trusted_bytes(path, owner_uid, 16384)
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix='.live-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, owner_uid, group_id if group_id is not None else -1)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode('ascii')


def build_configuration(monitor_content, station_callsign, tx_alsa_id='AIOC_TX'):
    station_callsign = guard.callsign(station_callsign)
    parser = guard.parser_from_bytes(monitor_content)
    if (set(parser.sections()) != {'GLOBAL', 'RepeaterLogic', 'RxAIOC'}
            or parser['GLOBAL'].get('LOGICS') != 'RepeaterLogic'
            or parser['RepeaterLogic'].get('RX') != 'RxAIOC'
            or parser['RepeaterLogic'].get('TX') != 'NONE'):
        raise ValueError('Prepare the mapped receive-only monitor before configuring transmission.')
    receiver = dict(parser['RxAIOC'])
    device = receiver.get('AUDIO_DEV', '')
    prefix, suffix = 'alsa:plughw:CARD=', ',DEV=0'
    rx_alsa_id = device[len(prefix):-len(suffix)] if device.startswith(prefix) and device.endswith(suffix) else ''
    if (guard.ALSA_RE.fullmatch(rx_alsa_id) is None or guard.ALSA_RE.fullmatch(tx_alsa_id) is None
            or rx_alsa_id == tx_alsa_id or 'REPLACE' in rx_alsa_id or 'REPLACE' in tx_alsa_id):
        raise ValueError('Use distinct, verified persistent RX and TX ALSA names.')
    output = guard.configparser.ConfigParser(interpolation=None)
    output.optionxform = str
    output['GLOBAL'] = guard.GLOBAL
    output['RepeaterLogic'] = dict(guard.LOGIC, CALLSIGN=station_callsign)
    output['RxAIOC'] = dict(guard.RX_FIXED, AUDIO_DEV=device)
    for key in guard.RX_TUNING:
        if key in receiver:
            output['RxAIOC'][key] = receiver[key]
    return output, rx_alsa_id, tx_alsa_id


def prepare(station_callsign, tx_alsa_id='AIOC_TX', tx_gain_db=-40, config_path=guard.CONFIG_PATH,
            auth_path=guard.AUTH_PATH, monitor_path=MONITOR_PATH,
            radio_config_path=guard.RADIO_CONFIG_PATH, state_path=guard.STATE_PATH,
            catalog_path=guard.CATALOG_PATH, owner_uid=0, auth_group_id=None,
            engine_group_id=None, device_check=guard.verify_runtime_devices):
    devices, radios, digest = guard.verified_station(
        guard.trusted_json(radio_config_path, owner_uid, 16384),
        guard.trusted_json(state_path, owner_uid), guard.trusted_json(catalog_path, owner_uid, 16384))
    parser, rx_id, tx_id = build_configuration(guard.trusted_bytes(monitor_path, owner_uid, 16384, confidential=False),
                                              station_callsign, tx_alsa_id)
    parser['TxAIOC'] = dict(guard.TX_FIXED, AUDIO_DEV='alsa:plughw:CARD=' + tx_id + ',DEV=0',
                           PTT_PORT=devices['tx'], MASTER_GAIN=guard.gain_text(tx_gain_db))
    output = io.StringIO()
    parser.write(output, space_around_delimiters=False)
    content = output.getvalue().encode('utf-8')
    authorization = {'schema': 1, 'enabled': False, 'callsign': station_callsign,
                     'devices': devices, 'radios': radios, 'rx_alsa_id': rx_id, 'tx_alsa_id': tx_id,
                     'tx_gain_db': guard.tx_gain(tx_gain_db),
                     'catalog_digest': digest, 'config_sha256': hashlib.sha256(content).hexdigest()}
    guard.validate_authorization(authorization, require_enabled=False)
    guard.validate_parser(parser, authorization)
    device_check(authorization)
    # Revoke any old authorization before replacing the engine configuration.
    atomic_write(auth_path, json_bytes(authorization), auth_group_id, owner_uid)
    atomic_write(config_path, content, engine_group_id, owner_uid)
    return authorization


def set_enabled(enabled, auth_path=guard.AUTH_PATH, owner_uid=0, group_id=None, **check_options):
    if enabled:
        authorization = guard.check(auth_path=auth_path, owner_uid=owner_uid,
                                    require_enabled=False, **check_options)
    else:
        try:
            authorization = guard.trusted_json(auth_path, owner_uid, 16384)
            if not isinstance(authorization, dict):
                authorization = {'schema': 1}
        except (FileNotFoundError, ValueError):
            authorization = {'schema': 1}
    authorization['enabled'] = bool(enabled)
    atomic_write(auth_path, json_bytes(authorization), group_id, owner_uid)
    return authorization


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--prepare', action='store_true', help='Prompt locally for a callsign and save a disabled live configuration')
    choice.add_argument('--enable', action='store_true', help='Authorize the prepared configuration; does not start or enable the service')
    choice.add_argument('--disable', action='store_true', help='Revoke authorization after stopping the live service')
    parser.add_argument('--tx-alsa-id', default='AIOC_TX')
    parser.add_argument('--tx-gain-db', type=float, default=-40,
                        help='Initial TX audio gain, -40 to -10 dB; adjust only during supervised audio checks')
    parser.add_argument('--confirm-ptt-tested', action='store_true')
    parser.add_argument('--confirm-audio-tested', action='store_true')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Run this local configuration command with sudo')
    descriptor = None
    try:
        descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('The local maintenance lock is not protected.')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        live_stopped()
        group_id = grp.getgrnam('repeaterdash').gr_gid
        if args.prepare:
            station_callsign = input('Station callsign (saved only on this Pi): ').strip().upper()
            prepare(station_callsign, args.tx_alsa_id, args.tx_gain_db, auth_group_id=group_id,
                    engine_group_id=grp.getgrnam('svxlink').gr_gid)
            print('Private live configuration prepared; transmission remains unauthorized and the service was not started.')
        elif args.enable:
            if not args.confirm_ptt_tested or not args.confirm_audio_tested:
                raise ValueError('Complete the supervised PTT and audio checks, then supply both confirmation flags.')
            set_enabled(True, group_id=group_id)
            print('Local transmission authorized. The service remains stopped; start it separately when ready.')
            print('Boot startup remains unchanged. This command does not enable the service.')
        else:
            set_enabled(False, group_id=group_id)
            print('Local transmission authorization revoked. The service remains stopped.')
        return 0
    except BlockingIOError:
        print('A maintenance operation is running. Retry after it finishes.', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, guard.configparser.Error) as error:
        print('Live configuration was not enabled: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == '__main__':
    raise SystemExit(main())
