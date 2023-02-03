import json
from os import access, R_OK
from os.path import isfile, join


def is_readable(file):
    return isfile(file) and access(file, R_OK)

root_dir = '/data/' if is_readable('/data/options.json') else ''
bms_meter_states = root_dir + 'bms_meter_states.json'


def load_meter_states():
    with open(bms_meter_states) as f:
        meter_states = json.load(f)
    return meter_states

def store_meter_states(meter_states):
    with open(bms_meter_states, 'w') as f:
        json.dump(meter_states, f)