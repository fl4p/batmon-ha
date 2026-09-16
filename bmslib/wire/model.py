"""The GUI wire format: two documents, one serializer.

PROVISIONAL -- not frozen. The schema is deliberately exercised by a real client
before it is committed to, because the mobile app will code against it
independently and additive-only compatibility is only cheap once it is right.

Two rules matter more than the field list:

1. An unknown SCALAR is an ABSENT KEY, never null. Unknown inside a positional
   ARRAY (cells_mv, temperatures_c) is null, because the index is the cell or
   sensor number and an array cannot have holes. That is the only null a client
   ever sees.
2. A state frame is WHOLE-NODE REPLACEMENT, not a merge. An absent field means
   "not reported now"; merging would resurrect a stale reading forever.

Imports: stdlib + bmslib.bms/bmslib.wire only. Enforced by test_wire_imports.py,
because this module also has to run on the phone.
"""
import hashlib
import json


import time

from bmslib.wire import topology
from bmslib.wire.fields import (sample_desc, meter_desc, round_to_n,
                                capitalize_words, is_none_or_nan, cell_stats)

WIRE_VERSION = 1

# Things publish_hass_discovery() hardcodes inline rather than taking from
# sample_desc. Kept here so the UI still needs no table of its own.
extra_desc = {
    'cells_mv': dict(unit='mV', device_class='voltage', precision=0,
                     icon='battery-outline', section='cells', array=True),
    'temperatures_c': dict(unit='°C', device_class='temperature', precision=1,
                           icon='thermometer', section='temps', array=True),
    'problem': dict(unit=None, device_class='problem', section='status',
                    icon='alert'),
    'problem_code': dict(unit=None, device_class=None, section='status'),
    'balancing_cells': dict(unit=None, device_class=None, section='status',
                            icon='scale-unbalanced', array=True),
    'battery_charging': dict(unit=None, device_class='battery_charging',
                             section='status'),
    'battery_mode': dict(unit=None, device_class=None, section='status'),
}


def _label(field):
    return capitalize_words(field.replace('_', ' '))


def field_catalog() -> dict:
    """field name -> display metadata, so the UI hardcodes no unit table.

    A new BmsSample field appears in both Home Assistant and the GUI by adding
    one sample_desc entry.
    """
    cat = {}
    for topic, d in sample_desc.items():
        f = d['field']
        cat[f] = {
            'unit': d.get('unit_of_measurement'),
            'device_class': d.get('device_class'),
            'state_class': d.get('state_class'),
            'precision': d.get('precision'),
            # 11 of the 16 entries have no significant_digits; publish_sample
            # defaults to 5 and so must we, or ordinary fields raise KeyError.
            'significant_digits': d.get('significant_digits', 5),
            'icon': d.get('icon'),
            'label': _label(f),
            'section': topic.split('/', 1)[0],
            'mqtt_topic': topic,
        }
    for name, d in meter_desc.items():
        cat['meter:' + name] = {
            'unit': d.get('unit'),
            'device_class': d.get('device_class'),
            'state_class': d.get('state_class'),
            'precision': 2,
            'significant_digits': 5,
            'icon': d.get('icon'),
            'label': capitalize_words(d.get('name') or _label(name)),
            'section': 'meter',
        }
    for f, d in extra_desc.items():
        e = dict(d)
        e.setdefault('state_class', None)
        e.setdefault('significant_digits', 5)
        e.setdefault('icon', None)
        e.setdefault('precision', None)
        e['label'] = _label(f[:-3] if f.endswith('_mv') or f.endswith('_c') else f)
        cat[f] = e
    return cat


_CATALOG = None


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = field_catalog()
    return _CATALOG


def sample_to_values(sample) -> dict:
    """BmsSample -> {attr: rounded value}, unknown keys omitted entirely."""
    cat = _catalog()
    out = {}
    for f, meta in cat.items():
        if f.startswith('meter:') or f in extra_desc:
            continue
        v = getattr(sample, f, None)
        if is_none_or_nan(v):
            continue
        out[f] = _round_num(v, meta.get('significant_digits', 5))
    n = getattr(sample, 'num_samples', None)
    if not is_none_or_nan(n):
        out['num_samples'] = n
    return out


def _round_num(v, sig):
    """Round exactly as publish_sample does, but return a JSON *number*.

    round_to_n() returns a str because MQTT payloads are text. A JSON API must
    emit numbers, or every consumer has to parse them back and charting breaks
    on a string. The numeric value is identical to what MQTT publishes.
    """
    r = round_to_n(v, sig)
    if isinstance(r, str):
        try:
            f = float(r)
        except ValueError:
            return r
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    return r


def _bitmask_to_cells(mask):
    if mask is None:
        return None
    return [i + 1 for i in range(mask.bit_length()) if mask >> i & 1]


def _clean_array(values, scale=1.0):
    """Positional array: unknown entries become null, not dropped."""
    out = []
    for v in values:
        out.append(None if is_none_or_nan(v) else (v * scale if scale != 1.0 else v))
    return out


def node_state(node_id, sample=None, voltages_mv=None, temperatures_c=None,
               meters=None, link=None, now=None) -> dict:
    now = time.time() if now is None else now
    st = {'link': link or {'status': 'never'}}

    if sample is not None:
        st['values'] = sample_to_values(sample)
        ts = getattr(sample, 'timestamp', None)
        if not is_none_or_nan(ts):
            st['sample_ts'] = ts
            st['age_s'] = round(now - ts, 3)
        sw = getattr(sample, 'switches', None)
        if sw:
            st['switches'] = {k: bool(v) for k, v in sw.items()}
        al = getattr(sample, 'alarms', None)
        if al:
            st['alarms'] = {k: bool(v) for k, v in al.items()}
        for f in ('problem', 'problem_code', 'battery_charging', 'battery_mode'):
            v = getattr(sample, f, None)
            if v is not None:
                st[f] = v
        bc = _bitmask_to_cells(getattr(sample, 'balancing_cells', None))
        if bc is not None:
            st['balancing_cells'] = bc
        temps = temperatures_c if temperatures_c is not None else getattr(sample, 'temperatures', None)
        if temps:
            st['temperatures_c'] = _clean_array(temps)
    elif temperatures_c:
        st['temperatures_c'] = _clean_array(temperatures_c)

    if voltages_mv:
        st['cells_mv'] = _clean_array(voltages_mv)
        known = [v for v in voltages_mv if not is_none_or_nan(v)]
        if len(known) > 1:
            st['cell_stats'] = cell_stats(known)

    if meters:
        m = {k: _round_num(v, 5) for k, v in meters.items() if not is_none_or_nan(v)}
        if m:
            st['meters'] = m

    st['stale'] = bool((link or {}).get('status') == 'stale')
    return st


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def system_document(nodes, producer, app_version, runtime, config_hash=None) -> dict:
    data = {
        'producer': producer,
        'app_version': app_version,
        'runtime': runtime,
        'fields': _catalog(),
    }
    data.update(topology.build(nodes))
    if config_hash is None:
        # must not hash itself, and must not include the envelope's ts
        config_hash = hashlib.sha1(_canonical(data).encode('utf-8')).hexdigest()[:12]
    data['config_hash'] = config_hash
    return {'v': WIRE_VERSION, 'type': 'system', 'ts': time.time(), 'data': data}


def state_document(node_states: dict, partial: bool) -> dict:
    return {'v': WIRE_VERSION, 'type': 'state', 'ts': time.time(),
            'data': {'partial': bool(partial), 'nodes': node_states}}


def encode(doc) -> str:
    """allow_nan=False on purpose: Python emits bare NaN, which is invalid JSON
    and breaks JSON.parse. A leaked NaN must raise here, in a test, rather than
    in someone's browser."""
    return json.dumps(doc, allow_nan=False, separators=(',', ':'))
