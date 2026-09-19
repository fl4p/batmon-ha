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


def load_user_config():
    try:
        with open('/data/options.json') as f:
            conf = dotdict(json.load(f))
            _user_config_migrate_addresses(conf)
    except Exception as e:
        logger.warning('error reading /data/options.json, trying options.json %s', e)
        with open('options.json') as f:
            conf = dotdict(json.load(f))
    _user_config_apply_global_adapter(conf)
    return conf


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
