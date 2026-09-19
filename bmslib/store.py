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
    controller (`hci1`) for a BLE device, a tty path (`/dev/ttyUSB0`) for a
    wired one -- so it is only handed to devices of the matching kind. A
    top-level value that fits no device is reported rather than dropped: that is
    the silent-ignore bug again, just with a different cause.
    """
    from bmslib.models import device_address, is_serial_device

    adapter = conf.get('adapter')
    if not isinstance(adapter, str) or not adapter.strip():
        return
    adapter = adapter.strip()
    # A serial port is a path, a BlueZ controller never is. Same discriminator
    # the two meanings have in the docs.
    is_port = '/' in adapter

    applied = []
    skipped = []
    for dev in (conf.get('devices') or []):
        if not isinstance(dev, dict) or dev.get('adapter'):
            continue
        name = dev.get('alias') or device_address(dev) or '?'
        if is_serial_device(dev) != is_port:
            skipped.append(name)
            continue
        dev['adapter'] = adapter
        applied.append(name)

    if applied:
        logger.info('applying top-level adapter=%s to %s (devices without their own `adapter:`)',
                    adapter, ', '.join(applied))
    else:
        if skipped:
            why = ('it looks like a serial port, but no wired device (address: serial) needs one'
                   if is_port else
                   'it looks like a Bluetooth controller, but no BLE device needs one')
        else:
            why = 'no configured device is missing an `adapter:`'
        logger.warning('top-level adapter=%s has no effect: %s. `adapter:` is a per-BMS option -- '
                       'put it inside the device entry under `devices:`.', adapter, why)


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
