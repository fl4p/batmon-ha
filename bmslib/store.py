import json
from os import access, R_OK
from os.path import isfile, join
from threading import Lock

from bmslib.util import dotdict, get_logger

logger = get_logger()


def is_readable(file):
    return isfile(file) and access(file, R_OK)

root_dir = '/data/' if is_readable('/data/options.json') else ''
bms_meter_states = root_dir + 'bms_meter_states.json'
lock = Lock()

def load_meter_states():
    with lock:
        with open(bms_meter_states) as f:
            meter_states = json.load(f)
        return meter_states

def store_meter_states(meter_states):
    with lock:
        with open(bms_meter_states, 'w') as f:
            json.dump(meter_states, f)


def load_user_config():
    try:
        with open('/data/options.json') as f:
            conf = dotdict(json.load(f))
            _user_config_migrate_addresses(conf)
    except Exception as e:
        print('error reading /data/options.json, trying options.json', e)
        with open('options.json') as f:
            conf = dotdict(json.load(f))
    return conf


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