#!/usr/bin/python3
"""Enable paired memory-1 changes only after local radio qualification."""
import argparse
import fcntl
import grp
import importlib.util
import os
from pathlib import Path
import stat
import sys

CONFIG = Path('/etc/repeater-box/radio-pair.json')


def validate_qualification(state, memory_one, live_reload, config=None):
    if memory_one is not True or live_reload is not True:
        raise ValueError('Confirm both radios stay on memory 1 and load changed frequencies without a power cycle')
    if not isinstance(state, dict) or any(isinstance(item, dict) and item.get('verification_required') for item in state.values()):
        raise ValueError('Resolve every unfinished radio upload before enabling paired changes')
    if config is not None and (not isinstance(config, dict) or type(config.get('schema')) is not int
                               or config['schema'] != 1 or config.get('model') != '5RX'
                               or not isinstance(config.get('devices'), dict)):
        raise ValueError('Use the current supported RX/TX configuration for qualification')
    for role in ('rx', 'tx'):
        item = state.get(role, {})
        if not isinstance(item, dict) or item.get('compatible') is not True or item.get('verified') is not True:
            raise ValueError('Program and verify both supported UV-5RX radios first')
        if config is not None and (not isinstance(config, dict) or not isinstance(config.get('devices'), dict)
                                   or item.get('device') != config['devices'].get(role)
                                   or not isinstance(item.get('device'), str)):
            raise ValueError('Inspect, program and verify the current RX/TX bindings before enabling paired changes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--enable', action='store_true')
    choice.add_argument('--disable', action='store_true')
    parser.add_argument('--confirm-memory-one', action='store_true', help='Both radios are in channel mode, display A, memory 1, with dual watch off')
    parser.add_argument('--confirm-live-reload', action='store_true', help='Bench-tested BOTH radios: changed frequency takes effect and a second programming operation works without touching/restarting the radios')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run locally with sudo after completing the bench checks')
    spec = importlib.util.spec_from_file_location('radio_worker', Path(__file__).with_name('program-radio.py'))
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    try:
        descriptor = os.open(worker.LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'r+') as lock:
            info = os.fstat(lock.fileno())
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('The local maintenance lock permissions are invalid')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.enable:
                state = worker.read_json(worker.BACKUP_DIR / 'state.json', private=True)
                config = worker.read_json(worker.CONFIG_PATH, private=True)
                if not worker.valid_config(config):
                    raise ValueError('Configure the supported RX and TX AIOC connections first')
                validate_qualification(state, args.confirm_memory_one, args.confirm_live_reload, config)
            worker.atomic_json(CONFIG, {'schema': 1, 'enabled': args.enable}, grp.getgrnam('repeaterdash').gr_gid)
        print('Paired changes enabled. Keep both box radios on memory 1.' if args.enable else 'Paired changes disabled. Existing unfinished uploads remain protected.')
        return 0
    except (OSError, ValueError, worker.ProgrammingError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
