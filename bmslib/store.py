import json
import os
import re
from os import access, R_OK
from os.path import isfile
from threading import Lock

from bmslib.cache import random_str
from bmslib.util import dotdict, get_logger

logger = get_logger()


def is_readable(file):
    return isfile(file) and access(file, R_OK)


root_dir = '/data/' if is_readable('/data/options.json') else ''
bms_meter_states_fn = root_dir + 'bms_meter_states.json'

lock = Lock()


def store_file(fn):
    return root_dir + fn


def load_meter_states():
    with lock:
        with open(bms_meter_states_fn) as f:
            meter_states = json.load(f)
        return meter_states


def store_meter_states(meter_states):
    with lock:
        s = f'.{random_str(6)}.tmp'
        with open(bms_meter_states_fn + s, 'w') as f:
            json.dump(meter_states, f, indent=2)
        os.replace(bms_meter_states_fn + s, bms_meter_states_fn)


def store_algorithm_state(bms_name, algorithm_name, state=None):
    fn = root_dir + 'bat_state_' + re.sub(r'[^\w_. -]', '_', bms_name) + '.json'
    with lock:
        with open(fn, 'a+') as f:
            try:
                f.seek(0)
                bms_state = json.load(f)
            except:
                logger.info('init %s bms state storage', bms_name)
                bms_state = dict(algorithm_state=dict())

            if state is not None:
                bms_state['algorithm_state'][algorithm_name] = state
                f.seek(0), f.truncate()
                json.dump(bms_state, f, indent=2)

            return bms_state['algorithm_state'].get(algorithm_name, None)


CONFIG_PATHS = ('/data/options.json', 'options.json')


class ConfigError(Exception):
    """A configuration file that cannot be used. Carries a message meant for the
    add-on log verbatim -- main.py prints it and exits, no traceback."""


def load_user_config():
    """Read the first configuration file that exists, or raise ConfigError.

    A file that exists but does not parse ABORTS: it used to be logged as a
    warning and then skipped in favour of the next path, which is how a typo in
    `/data/options.json` surfaced as `No such file or directory: 'options.json'`
    -- an error naming a file the user never wrote, with the actual syntax error
    (a missing comma, in #414) buried in an earlier warning line. Where a second
    file does exist, silently running a different configuration than the one that
    was edited is worse still.
    """
    tried = []
    for path in CONFIG_PATHS:
        if not isfile(path):
            tried.append('%s (not found)' % path)
            continue
        if not access(path, R_OK):
            raise ConfigError('cannot read configuration file %s: permission denied' % path)
        with open(path) as f:
            text = f.read()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigError(_json_error_report(path, text, e)) from None
        if not isinstance(parsed, dict):
            raise ConfigError('%s must contain a JSON object ({...}), found %s'
                              % (path, type(parsed).__name__))
        conf = dotdict(parsed)
        logger.info('reading configuration from %s', path)
        # Runs for every path now. The legacy `<slug>_address` keys it migrates
        # are not specific to the add-on file, and a standalone options.json
        # carrying them used to be skipped for no reason.
        _user_config_migrate_addresses(conf)
        _user_config_apply_global_adapter(conf)
        return conf

    raise ConfigError('no configuration file: tried %s. See doc/Docker.md for how to mount one.'
                      % ', '.join(tried))


def _json_error_report(path: str, text: str, e: json.JSONDecodeError) -> str:
    """Point at the offending character, the way a compiler would.

    json's own message ("Expecting ',' delimiter: line 7 column 3 (char 142)")
    is accurate but easy to lose in a log; the quoted line and caret are what
    make a missing comma obvious without opening an editor.
    """
    lines = text.splitlines()
    src = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ''
    report = ['%s is not valid JSON: %s (line %d, column %d)' % (path, e.msg, e.lineno, e.colno)]
    if src:
        report.append('    %s' % src)
        report.append('    %s^' % (' ' * (e.colno - 1)))
    if "','" in e.msg or "':'" in e.msg:
        # The overwhelmingly common cause, and the one behind #414.
        report.append('A missing or extra comma between two options is the usual cause. '
                      'Every entry but the last needs a trailing comma.')
    report.append('Fix the file and restart. Nothing was loaded -- batmon does not fall back to '
                  'another configuration, that would run settings you did not edit.')
    return '\n'.join(report)


def _user_config_apply_global_adapter(conf):
    """Let a top-level `adapter:` act as the default for devices that have none.

    `adapter:` is documented as a per-BMS option, so a top-level one used to be
    read by nobody at all: the add-on kept using the default controller and
    never said why, which is exactly how it looks when the chosen adapter fails
    (#414). Inheriting it is what the setting plainly means, and the log line
    makes the inheritance visible instead of magic.

    The value means two different things depending on the transport -- a BlueZ
    controller (`hci1` or its MAC) for a BLE device, a serial port
    (`/dev/ttyUSB0`) for a wired one -- so it is only handed to devices of the
    matching kind, recognized by _global_adapter_kind(). Anything this function
    cannot place -- an unrecognizable value, a value whose kind no device needs,
    a non-string -- is reported rather than dropped: staying quiet is the
    silent-ignore bug again, just with a different cause.
    """
    from bmslib.models import device_address, is_serial_device

    adapter = conf.get('adapter')
    if adapter is None:
        return
    if not isinstance(adapter, str) or not adapter.strip():
        # A number, a bool, a list, or "" -- the user did set something.
        if adapter != '':
            logger.warning('ignoring top-level adapter=%r: expected a Bluetooth controller '
                           '("hci1" or its MAC) or a serial port ("/dev/ttyUSB0")', adapter)
        return
    adapter = adapter.strip()

    kind = _global_adapter_kind(adapter)
    if kind is None:
        logger.warning('ignoring top-level adapter=%s: not recognizable as a Bluetooth controller '
                       '("hci1" or its MAC) or as a serial port (an absolute path like '
                       '"/dev/ttyUSB0"). Set it per device under `devices:` if you mean something '
                       'else.', adapter)
        return

    applied = []
    overridden = []
    mismatched = []
    for dev in (conf.get('devices') or []):
        if not isinstance(dev, dict):
            continue
        address = device_address(dev)
        # Same skip rules as construct_bms(): a commented-out or empty address is
        # not a device, and a group has no transport of its own (its members do).
        # Handing those an adapter would put a controller nobody connects with
        # into main.py's start-up discovery sweep.
        if not address or address.startswith('#') or _is_group_device(dev):
            continue
        if dev.get('adapter'):
            overridden.append(dev.get('alias') or address)
            continue
        name = dev.get('alias') or address
        if is_serial_device(dev) != (kind == 'port'):
            mismatched.append(name)
            continue
        dev['adapter'] = adapter
        applied.append(name)

    if applied:
        logger.info('applying top-level adapter=%s to %s (devices without their own `adapter:`)',
                    adapter, ', '.join(applied))
    elif overridden and not mismatched:
        # A default that every device overrides is a legitimate config, not a mistake.
        logger.info('top-level adapter=%s is unused: %s set their own `adapter:`',
                    adapter, ', '.join(overridden))
    else:
        why = ('it is a serial port, but no wired device (address: serial) is missing one'
               if kind == 'port' else
               'it is a Bluetooth controller, but no BLE device is missing one')
        logger.warning('top-level adapter=%s has no effect: %s. `adapter:` can also be set per '
                       'BMS inside the device entry under `devices:`.', adapter, why)


# "hci1" or the controller MAC that normalize_adapter() resolves to one (bt.py).
_BT_ADAPTER_RE = re.compile(r'^(hci\d+|([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})$')


def _global_adapter_kind(adapter: str):
    """'bt', 'port', or None when the value fits neither.

    Deliberately not "a path means serial, anything else means Bluetooth": a
    relative port name like `ttyUSB0` is a path serial.Serial accepts, and under
    that rule it would have been handed to every BLE device instead, silently.
    An unplaceable value is refused out loud rather than guessed at.
    """
    if _BT_ADAPTER_RE.match(adapter):
        return 'bt'
    if adapter.startswith('/'):
        return 'port'
    return None


def _is_group_device(dev: dict) -> bool:
    """True for `type: group_parallel` / `group_serial` (bmslib.models.BMS_TYPES).

    A group is an aggregate over other devices; it opens no link of its own, so
    `adapter:` means nothing to it (bmslib/group.py).
    """
    return str(dev.get('type') or '').strip().split(':', 1)[0].startswith('group_')


def _user_config_migrate_addresses(conf):
    changed = False
    slugs = ["daly", "jbd", "jk", "sok", "victron"]
    conf["devices"] = conf.get('devices') or []
    devices_by_address = {d['address']: d for d in conf["devices"]}
    for slug in slugs:
        addr = conf.get(f'{slug}_address')
        if addr and not devices_by_address.get(addr):
            device = dict(
                address=addr.strip('?'),
                type=slug,
                alias=slug + '_bms',
            )
            if addr.endswith('?'):
                device["debug"] = True
            if conf.get(f'{slug}_pin'):
                device['pin'] = conf.get(f'{slug}_pin')
            conf["devices"].append(device)
            del conf[f'{slug}_address']
            logger.info('Migrated %s_address to device %s', slug, device)
            changed = True
    if changed:
        logger.info('Please update add-on configuration manually.')
