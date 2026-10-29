#!/usr/bin/python3
"""Validate a locally authorized RF service without opening a radio device."""
import configparser
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from radio_plan import catalog_digest, pair_plans, validate_catalog

CONFIG_PATH = Path('/etc/repeater-box/svxlink-live.conf')
AUTH_PATH = Path('/etc/repeater-box/live.json')
RADIO_CONFIG_PATH = Path('/etc/repeater-box/radio-programming.json')
STATE_PATH = Path('/var/lib/repeater-radio-backups/state.json')
CATALOG_PATH = Path('/etc/repeater-box/channel-pairs.json')
EVENT_HANDLER = Path('/opt/repeater-box/scripts/repeater-live-events.tcl')
DTMF_HANDLER = Path('/opt/repeater-box/scripts/repeater-box-events.tcl')
DEVICE_RE = re.compile(r'/dev/serial/by-id/usb-AIOC_All-In-One-Cable_([A-Za-z0-9_-]{1,64})-if04\Z')
PROGRAMMER_RE = re.compile(r'/dev/serial/by-(?:id|path)/[A-Za-z0-9_.:-]{1,200}\Z')
ALSA_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,31}\Z')
DIGEST_RE = re.compile(r'[0-9a-f]{64}\Z')
MORSE = dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/',
    ('.-', '-...', '-.-.', '-..', '.', '..-.', '--.', '....', '..', '.---', '-.-', '.-..', '--',
     '-.', '---', '.--.', '--.-', '.-.', '...', '-', '..-', '...-', '.--', '-..-', '-.--', '--..',
     '-----', '.----', '..---', '...--', '....-', '.....', '-....', '--...', '---..', '----.', '-..-.')))

GLOBAL = {'LOGICS': 'RepeaterLogic', 'TIMESTAMP_FORMAT': '"%Y-%m-%d %H:%M:%S"',
          'CARD_SAMPLE_RATE': '48000', 'CARD_CHANNELS': '1'}
LOGIC = {'TYPE': 'Repeater', 'RX': 'RxAIOC', 'TX': 'TxMonitored', 'MODULES': '',
         'ONLINE': '1', 'NO_REPEAT': '0', 'EVENT_HANDLER': str(EVENT_HANDLER),
         'DEFAULT_LANG': 'en_US', 'SHORT_IDENT_INTERVAL': '0', 'LONG_IDENT_INTERVAL': '0',
         'RGR_SOUND_DELAY': '-1', 'IDLE_SOUND_INTERVAL': '0', 'IDENT_NAG_TIMEOUT': '0',
         'IDLE_TIMEOUT': '1', 'OPEN_ON_SQL': '100', 'OPEN_SQL_FLANK': 'OPEN',
         'STATE_PTY': '/run/repeater-box/svxlink-state',
         'COMMAND_PTY': '/run/repeater-box/svxlink-command', 'EXEC_CMD_ON_SQL_CLOSE': '500'}
RX_FIXED = {'TYPE': 'Local', 'AUDIO_CHANNEL': '0', 'SQL_DET': 'VOX', 'SIGLEV_DET': 'NONE',
            'DEEMPHASIS': '0', 'PEAK_METER': '1', 'DTMF_DEC_TYPE': 'INTERNAL',
            'DTMF_MUTING': '1', 'DTMF_HANGTIME': '40', 'RAW_AUDIO_UDP_DEST': '127.0.0.1:7355'}
RX_TUNING = {'SQL_START_DELAY': (0, 5000), 'SQL_DELAY': (0, 5000),
             'SQL_HANGTIME': (0, 5000), 'VOX_FILTER_DEPTH': (1, 1000),
             'VOX_THRESH': (1, 32767), 'PREAMP': (-40, 40)}
TX_FIXED = {'TYPE': 'Local', 'AUDIO_CHANNEL': '0', 'LIMITER_THRESH': '-9', 'PREEMPHASIS': '0',
            'PTT_TYPE': 'SerialPin', 'PTT_PIN': 'DTR', 'SERIAL_SET_PINS': '!DTR!RTS',
            'TIMEOUT': '40', 'PTT_HANGTIME': '0', 'TX_DELAY': '300'}
TX_COMPOSITE = {'TYPE': 'Multi', 'TRANSMITTERS': 'TxAIOC,TxMonitor'}
TX_MONITOR = {'TYPE': 'Local', 'AUDIO_DEV': 'udp:127.0.0.1:7356', 'AUDIO_CHANNEL': '0',
              'PTT_TYPE': 'NONE', 'PREEMPHASIS': '0', 'MASTER_GAIN': '0',
              'LIMITER_THRESH': '-9', 'TIMEOUT': '40', 'TX_DELAY': '300', 'PTT_HANGTIME': '0'}


class DevicesNotReady(ValueError):
    """A configured interface or its sysfs attributes have not appeared yet."""


def callsign(value):
    if (not isinstance(value, str) or not 3 <= len(value) <= 15
            or re.fullmatch(r'[A-Z0-9]+(?:/[A-Z0-9]+)?', value) is None
            or not re.search(r'[A-Z]', value) or not re.search(r'[0-9]', value)
            or value.split('/')[0] == 'N0CALL' or value.startswith('REPLACE')):
        raise ValueError('Enter your station callsign locally, using uppercase letters, digits and an optional / suffix.')
    units = sum(sum(1 if symbol == '.' else 3 for symbol in MORSE[letter])
                + len(MORSE[letter]) - 1 for letter in value) + 3 * (len(value) - 1)
    if units > 166:
        raise ValueError('This callsign takes more than ten seconds at the initial CW speed; review its form before commissioning.')
    return value


def tx_gain(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not -40 <= value <= -10:
        raise ValueError('TX audio gain must be a finite number between -40 and -10 dB.')
    return float(value)


def gain_text(value):
    return format(tx_gain(value), '.12g')


def id_interval(value):
    if type(value) is not int or value not in (9, 30):
        raise ValueError('Choose nine minutes for normal identification or thirty minutes for supervised testing.')
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate local configuration field.')
        result[key] = value
    return result


def trusted_bytes(path, owner_uid=0, maximum=262144, confidential=True):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                or info.st_mode & (0o027 if confidential else 0o022) or info.st_size > maximum):
            raise ValueError('A local RF configuration has unsafe ownership, permissions or size.')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('A local RF configuration is too large.')
        return data
    finally:
        os.close(descriptor)


def trusted_json(path, owner_uid=0, maximum=262144):
    return json.loads(trusted_bytes(path, owner_uid, maximum), object_pairs_hook=unique_object)


def parser_from_bytes(content):
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=('#', ';'))
    parser.optionxform = str
    parser.read_string(content.decode('utf-8'))
    if parser.defaults():
        raise ValueError('RF configuration defaults are not supported.')
    return parser


def radio_bindings(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'model', 'devices'}
            or type(value['schema']) is not int or value['schema'] != 1 or value['model'] != '5RX'
            or not isinstance(value['devices'], dict)
            or not {'rx', 'tx'} <= set(value['devices']) <= {'rx', 'tx', 'programmer'}):
        raise ValueError('Configure the supported RX and TX radio connections first.')
    result = {role: value['devices'][role] for role in ('rx', 'tx')}
    if any(not isinstance(device, str) or DEVICE_RE.fullmatch(device) is None for device in result.values()) or result['rx'] == result['tx']:
        raise ValueError('RX and TX require separate persistent AIOC connections.')
    programmer = value['devices'].get('programmer')
    if ('programmer' in value['devices'] and (not isinstance(programmer, str)
            or PROGRAMMER_RE.fullmatch(programmer) is None)
            or len(set(value['devices'].values())) != len(value['devices'])):
        raise ValueError('The optional external programmer binding is invalid.')
    return result


def verified_station(radio_config, state, catalog):
    devices = radio_bindings(radio_config)
    catalog = validate_catalog(catalog)
    if (not isinstance(state, dict) or not state
            or any(not isinstance(item, dict) for item in state.values())
            or any(key in item and type(item[key]) is not bool
                   for item in state.values() for key in ('verification_required', 'release_verification_required'))
            or any(item.get('verification_required') or item.get('release_verification_required')
                   for item in state.values())):
        raise ValueError('Resolve every unfinished radio upload before enabling transmission.')
    radios = {}
    for role in ('rx', 'tx'):
        item = state.get(role, {})
        identity, firmware = item.get('identity'), item.get('firmware')
        if (item.get('verified') is not True or item.get('compatible') is not True or item.get('programmed') is not True
                or item.get('device') != devices[role]
                or not isinstance(identity, str) or re.fullmatch(r'[0-9a-f]{8,64}', identity) is None
                or not isinstance(firmware, str) or not 1 <= len(firmware) <= 80):
            raise ValueError('Both currently bound box radios must be inspected, programmed and verified.')
        radios[role] = {'identity': identity, 'firmware': firmware}
    pair = state.get('_pair', {})
    if (pair.get('stored_verified') is not True or pair.get('phase') != 'configured'
            or pair.get('activation_required') is not False or pair.get('catalog_digest') != catalog_digest(catalog)
            or pair.get('pair_id') not in pair_plans(catalog) or pair.get('devices') != devices
            or not isinstance(pair.get('roles'), dict) or not isinstance(pair.get('backup_ids'), dict)):
        raise ValueError('Program and verify a complete pair from the current private channel catalog first.')
    for role in ('rx', 'tx'):
        status = pair['roles'].get(role)
        if (not isinstance(status, dict) or set(status) != {'written', 'verified'}
                or status['written'] is not True or status['verified'] is not True
                or state[role].get('program_kind') != 'pair'
                or state[role].get('program_id') != pair['backup_ids'].get(role)
                or not isinstance(state[role].get('program_id'), str)
                or re.fullmatch(r'[0-9a-f]{32}', state[role]['program_id']) is None):
            raise ValueError('The current pair verification record is incomplete.')
    return devices, radios, catalog_digest(catalog)


def validate_authorization(value, require_enabled=True):
    if not isinstance(value, dict) or (require_enabled and value.get('enabled') is not True):
        raise ValueError('Transmission is not authorized. Complete local live configuration first.')
    expected = {'schema', 'enabled', 'callsign', 'devices', 'radios', 'rx_alsa_id', 'tx_alsa_id', 'tx_gain_db',
                'id_interval_minutes',
                'catalog_digest', 'config_sha256'}
    if (set(value) != expected or type(value.get('schema')) is not int or value['schema'] != 1
            or type(value.get('enabled')) is not bool
            or not isinstance(value.get('devices'), dict) or set(value['devices']) != {'rx', 'tx'}
            or not isinstance(value.get('radios'), dict) or set(value['radios']) != {'rx', 'tx'}
            or any(not isinstance(value.get(key), str) or DIGEST_RE.fullmatch(value[key]) is None
                   for key in ('catalog_digest', 'config_sha256'))
            or any(not isinstance(value.get(key), str) or ALSA_RE.fullmatch(value[key]) is None
                   for key in ('rx_alsa_id', 'tx_alsa_id')) or value['rx_alsa_id'] == value['tx_alsa_id']):
        raise ValueError('The local transmission authorization is invalid. Prepare it again.')
    callsign(value['callsign'])
    tx_gain(value['tx_gain_db'])
    id_interval(value['id_interval_minutes'])
    radio_bindings({'schema': 1, 'model': '5RX', 'devices': value['devices']})
    for item in value['radios'].values():
        if (not isinstance(item, dict) or set(item) != {'identity', 'firmware'}
                or not isinstance(item['identity'], str) or re.fullmatch(r'[0-9a-f]{8,64}', item['identity']) is None
                or not isinstance(item['firmware'], str) or not 1 <= len(item['firmware']) <= 80):
            raise ValueError('The authorized radio identity is invalid.')
    return value


def exact_section(parser, section, expected):
    if dict(parser[section]) != expected:
        raise ValueError('The live RF configuration differs from its fixed local operating profile.')


def _validate_profile(parser, authorization, *, legacy):
    sections = {'GLOBAL', 'RepeaterLogic', 'RxAIOC', 'TxAIOC'}
    if not legacy:
        sections.update(('TxMonitored', 'TxMonitor'))
    if set(parser.sections()) != sections or parser.defaults():
        raise ValueError('Only the fixed local repeater and its restricted audio sections are supported.')
    exact_section(parser, 'GLOBAL', GLOBAL)
    exact_section(parser, 'RepeaterLogic', dict(LOGIC, TX='TxAIOC' if legacy else 'TxMonitored',
                  CALLSIGN=authorization['callsign'],
                  BOX_ID_INTERVAL_MINUTES=str(id_interval(authorization['id_interval_minutes']))))
    rx = dict(parser['RxAIOC'])
    expected_rx = dict(RX_FIXED, AUDIO_DEV='alsa:plughw:CARD=' + authorization['rx_alsa_id'] + ',DEV=0')
    if not set(expected_rx) <= set(rx) or set(rx) - set(expected_rx) - set(RX_TUNING):
        raise ValueError('The live receiver contains unsupported settings.')
    for key, expected in expected_rx.items():
        if rx[key] != expected:
            raise ValueError('The live receiver differs from its restricted audio profile.')
    for key, value in rx.items():
        if key in RX_TUNING:
            low, high = RX_TUNING[key]
            if key != 'PREAMP' and re.fullmatch(r'[0-9]+', value) is None:
                raise ValueError('Receiver timing and VOX settings must be whole numbers.')
            try:
                number = float(value)
            except ValueError:
                raise ValueError('Invalid local receiver calibration.') from None
            if not low <= number <= high:
                raise ValueError('Local receiver calibration is outside supported bounds.')
    if not {'VOX_THRESH', 'VOX_FILTER_DEPTH', 'SQL_HANGTIME'} <= set(rx):
        raise ValueError('The live receiver is missing its local squelch calibration.')
    exact_section(parser, 'TxAIOC', dict(TX_FIXED, TIMEOUT='15' if legacy else '40',
        AUDIO_DEV='alsa:plughw:CARD=' + authorization['tx_alsa_id'] + ',DEV=0',
        PTT_PORT=authorization['devices']['tx'], MASTER_GAIN=gain_text(authorization['tx_gain_db'])))
    if not legacy:
        exact_section(parser, 'TxMonitored', TX_COMPOSITE)
        exact_section(parser, 'TxMonitor', TX_MONITOR)


def validate_parser(parser, authorization):
    """Accept only the current profile for normal authorization and startup."""
    _validate_profile(parser, authorization, legacy=False)


def _validate_legacy_parser(parser, authorization):
    """Recognize the exact previous profile for offline migration only."""
    _validate_profile(parser, authorization, legacy=True)


def usb_identity(path):
    try:
        device = path.resolve(strict=True)
        for parent in (device, *device.parents):
            if (parent / 'idVendor').is_file():
                return tuple((parent / name).read_text().strip() for name in ('idVendor', 'idProduct', 'serial'))
    except FileNotFoundError:
        raise DevicesNotReady('A configured radio interface USB identity is not available yet.') from None
    raise DevicesNotReady('A configured radio interface USB identity is not available yet.')


def serial_usb_identity(path, tty_root=Path('/sys/class/tty')):
    try:
        device_name = Path(path).resolve(strict=True).name
    except FileNotFoundError:
        raise DevicesNotReady('A configured AIOC serial connection is not available yet.') from None
    return usb_identity(tty_root / device_name / 'device')


def verify_runtime_devices(authorization, sound_root=Path('/sys/class/sound')):
    """Read device identity only; never open an audio or serial connection."""
    devices = authorization['devices']
    unavailable = False
    available_serial = set()
    for role, path in devices.items():
        try:
            info = os.stat(path)
        except FileNotFoundError:
            unavailable = True
            continue
        if not stat.S_ISCHR(info.st_mode):
            raise ValueError('A configured AIOC serial connection is not available.')
        available_serial.add(role)
    if len(available_serial) == 2:
        try:
            duplicate = os.path.samefile(devices['rx'], devices['tx'])
        except FileNotFoundError:
            unavailable = True
        else:
            if duplicate:
                raise ValueError('The two radio connections resolve to the same AIOC.')
    for role in ('rx', 'tx'):
        expected_id = authorization[role + '_alsa_id']
        expected_serial = DEVICE_RE.fullmatch(devices[role]).group(1)
        expected = ('1209', '7388', expected_serial)
        if role in available_serial:
            try:
                identity = serial_usb_identity(devices[role])
            except DevicesNotReady:
                unavailable = True
            else:
                if identity != expected:
                    raise ValueError('A serial radio connection does not match its authorized physical AIOC.')
        matching = 0
        for card in sound_root.glob('card[0-9]*'):
            try:
                if (card / 'id').read_text().strip() != expected_id:
                    continue
            except FileNotFoundError:
                continue
            matching += 1
            if matching > 1:
                raise ValueError('An ALSA radio name does not match its authorized physical AIOC.')
            try:
                identity = usb_identity(card / 'device')
            except DevicesNotReady:
                unavailable = True
            else:
                if identity != expected:
                    raise ValueError('An ALSA radio name does not match its authorized physical AIOC.')
        if matching == 0:
            unavailable = True
    if unavailable:
        raise DevicesNotReady('Configured AIOC serial connections and ALSA names are not ready yet.')


def check(config_path=CONFIG_PATH, auth_path=AUTH_PATH, radio_config_path=RADIO_CONFIG_PATH,
          state_path=STATE_PATH, catalog_path=CATALOG_PATH, owner_uid=0,
          require_enabled=True, device_check=verify_runtime_devices,
          event_handler=EVENT_HANDLER, dtmf_handler=DTMF_HANDLER):
    authorization = validate_authorization(trusted_json(auth_path, owner_uid, 16384), require_enabled)
    devices, radios, digest = verified_station(trusted_json(radio_config_path, owner_uid, 16384),
        trusted_json(state_path, owner_uid), trusted_json(catalog_path, owner_uid, 16384))
    if (authorization['devices'] != devices or authorization['radios'] != radios
            or authorization['catalog_digest'] != digest):
        raise ValueError('The authorized radio bindings or private catalog changed. Prepare transmission again.')
    content = trusted_bytes(config_path, owner_uid, 16384)
    if hashlib.sha256(content).hexdigest() != authorization['config_sha256']:
        raise ValueError('The local live configuration changed. Prepare transmission again.')
    validate_parser(parser_from_bytes(content), authorization)
    trusted_bytes(event_handler, owner_uid, 65536, confidential=False)
    trusted_bytes(dtmf_handler, owner_uid, 65536, confidential=False)
    device_check(authorization)
    return authorization


def check_when_ready(wait_seconds=0, clock=time.monotonic, pause=time.sleep, **check_options):
    """Bound enumeration waits while rechecking every RF interlock each time."""
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
        raise ValueError('Device readiness wait must be between zero and thirty seconds.')
    deadline = clock() + wait_seconds
    announced = False
    while True:
        try:
            return check(**check_options)
        except DevicesNotReady:
            if wait_seconds == 0:
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise DevicesNotReady('Configured AIOC devices were not ready within the startup deadline.') from None
            if not announced:
                print('Waiting briefly for the configured AIOC connections and ALSA names.', flush=True)
                announced = True
            pause(min(0.5, remaining))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv in ([], [str(CONFIG_PATH)]):
        wait_seconds = 0
    elif argv in (['--wait-for-devices', '30'], [str(CONFIG_PATH), '--wait-for-devices', '30']):
        wait_seconds = 30
    else:
        raise ValueError('Use only the installed local live configuration and optional --wait-for-devices 30.')
    check_when_ready(wait_seconds)
    print('Local live authorization, radio verification and AIOC identities checked.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, configparser.Error) as error:
        sys.exit(str(error))
