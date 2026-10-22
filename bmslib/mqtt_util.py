"""

HA mdi: icons https://pictogrammers.com/library/mdi/


"""
import asyncio
import json
import math
import queue
import time
import traceback
from unittest.mock import patch

import paho.mqtt.client as paho

from bmslib.bms import BmsSample, DeviceInfo, MIN_VALUE_EXPIRY
from bmslib.bt import BtBms
from bmslib.models.jk_settings import LABELS, SETTINGS, format_value
from bmslib.util import get_logger
from bmslib.wire.fields import (round_to_n, capitalize_words, is_none_or_nan,
                                balancing_cells_str, sample_desc, meter_desc, cell_stats)  # noqa: F401

logger = get_logger()

no_publish_fail_warn = False


# We need to ensure that c encoder will not be launched
@patch('json.encoder.c_make_encoder', None)
def json_dumps_with_round_n(some_object, n=7):
    # saving original method
    of = json.encoder._make_iterencode

    def inner(*args, **kwargs):
        args = list(args)
        # fifth argument is float formater which will we replace
        args[4] = lambda o: str(round_to_n(o, n))
        return of(*args, **kwargs)

    with patch('json.encoder._make_iterencode', wraps=inner):
        return json.dumps(some_object)


def disable_warnings():
    global no_publish_fail_warn
    no_publish_fail_warn = True


def remove_none_values(fields: dict):
    for k in list(fields.keys()):
        v = fields[k]
        if v is None:
            del fields[k]
        elif isinstance(v, float):
            if math.isnan(v) or not math.isfinite(v):
                del fields[k]
        elif isinstance(v, str):
            if not v:
                del fields[k]


def remove_equal_values(fields: dict, other: dict):
    if not other:
        return
    for k in list(fields.keys()):
        if k in other and fields[k] == other[k]:
            del fields[k]


_last_values = {}
_last_publish_time = 0.


def mqtt_single_out(client: paho.Client, topic, data, retain=False):
    # logger.debug(f'Send data: {data} on topic: {topic}, retain flag: {retain}')
    # print('mqtt: ' + topic, data)
    # return

    if client is None:
        # print('mqtt: ' + topic, data)
        return

    lv = _last_values.get(topic, None)
    if lv and lv[1] == data and (time.time() - lv[0]) < (MIN_VALUE_EXPIRY / 2):
        logger.debug('topic %s data not changed', topic)
        return False

    mqi: paho.MQTTMessageInfo = client.publish(topic, data, retain=retain)
    if mqi.rc != paho.MQTT_ERR_SUCCESS:
        if not no_publish_fail_warn:
            logger.warning('mqtt publish %s failed: %s %s', topic, mqi.rc, mqi)
        return False

    now = time.time()
    _last_values[topic] = now, data
    global _last_publish_time
    _last_publish_time = now


def mqtt_last_publish_time():
    global _last_publish_time
    return _last_publish_time


def publish_sample(client, device_topic, sample: BmsSample):
    for k, v in sample_desc.items():
        topic = f"{device_topic}/{k}"
        s = round_to_n(getattr(sample, v['field']), v.get('significant_digits', 5))
        if not is_none_or_nan(s):
            mqtt_single_out(client, topic, s)

    if sample.switches:
        for switch_name, switch_state in sample.switches.items():
            assert isinstance(switch_state, bool)
            topic = f"{device_topic}/switch/{switch_name}"
            mqtt_single_out(client, topic, 'ON' if switch_state else 'OFF')

    if sample.alarms:
        for alarm_name, alarm_state in sample.alarms.items():
            assert isinstance(alarm_state, bool)
            topic = f"{device_topic}/alarm/{alarm_name}"
            mqtt_single_out(client, topic, 'ON' if alarm_state else 'OFF')

    if sample.problem is not None:
        mqtt_single_out(client, f"{device_topic}/problem",
                        'ON' if sample.problem else 'OFF')
    if sample.problem_code is not None:
        mqtt_single_out(client, f"{device_topic}/problem_code", sample.problem_code)

    if sample.balancing_cells is not None:
        mqtt_single_out(client, f"{device_topic}/balancing", 'ON' if sample.balancing_cells else 'OFF')
        mqtt_single_out(client, f"{device_topic}/balancing_cells", balancing_cells_str(sample.balancing_cells))

    if sample.battery_charging is not None:
        mqtt_single_out(client, f"{device_topic}/battery_charging",
                        'ON' if sample.battery_charging else 'OFF')
    if sample.battery_mode is not None:
        mqtt_single_out(client, f"{device_topic}/battery_mode", sample.battery_mode)

    for key, value in sample.extra_values.items():
        if not is_none_or_nan(value):
            mqtt_single_out(client, f"{device_topic}/{key}", value)


def publish_cell_voltages(client, device_topic, voltages):
    # "highest_voltage": parts[0] / 1000,
    # "highest_cell": parts[1],
    # "lowest_voltage": parts[2] / 1000,
    # "lowest_cell": parts[3],

    if not voltages:
        return

    for i in range(0, len(voltages)):
        topic = f"{device_topic}/cell_voltages/{i + 1}"
        mqtt_single_out(client, topic, voltages[i] / 1000)

    stats = cell_stats(voltages)
    if stats:
        mqtt_single_out(client, f"{device_topic}/cell_voltages/min", stats['min_mv'] / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/min_index", stats['min_index'])
        mqtt_single_out(client, f"{device_topic}/cell_voltages/max", stats['max_mv'] / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/max_index", stats['max_index'])
        mqtt_single_out(client, f"{device_topic}/cell_voltages/delta", stats['delta_mv'] / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/average", stats['avg_mv'] / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/median", stats['median_mv'] / 1000)


def publish_temperatures(client, device_topic, temperatures):
    if not temperatures:
        return
    for i in range(0, len(temperatures)):
        topic = f"{device_topic}/temperatures/{i + 1}"
        if not is_none_or_nan(temperatures[i]):
            mqtt_single_out(client, topic, round_to_n(temperatures[i], 4))


# An accepted impedance window is published once, not refreshed every cycle, so
# the entity needs an expiry of its own: a day without a new accepted window and
# HA shows it unavailable instead of an old number that looks current.
CELL_RESISTANCE_EXPIRE_S = 24 * 3600


def publish_cell_resistance(client, device_topic, value_mohm: float):
    mqtt_single_out(client, f"{device_topic}/cell_resistance", round_to_n(value_mohm, 3))


# Qmax/SoH (bmslib/qmax.py) is published once per accepted segment -- weeks
# apart at best -- and never refreshed. The entity expires with the estimate
# itself: a year after the newest segment, the age limit of the median. An
# estimate that may no longer go out is withdrawn before that (withdraw_qmax).
QMAX_EXPIRE_S = 365 * 86400

# offset_drift_pct is the drift of the ASSUMED offset offset_assumed_a (a tuning
# constant or the rest reading), not a bound on the error (bmslib/qmax.py).
# anchors: what the newest segment ran between ('ocv->top': a rest near empty,
# then a charge termination, doc/SoH.md); top_anchored: how many of the counted
# segments used a top anchor, whose 100 % is full as the charger defines it.
QMAX_ATTRIBUTES = ('segments', 'newest', 'limiting_cell', 'cell_spread_pct', 'min_dsoc', 'offset_assumed_a',
                   'offset_drift_pct', 'capacity', 'capacity_source', 'plausibility_checked', 'anchors',
                   'top_anchored')


# A withdrawal is sent again this often for as long as nothing may be published.
# Home Assistant restores an MQTT sensor's last state at its start (until
# expire_after, a year here), so one that missed the withdrawal (it was down, or
# the broker was) shows the old estimate again; an hour later it is gone. Not
# retained: a retained value outlives the estimator in the broker (switched off,
# the BMS removed) and comes back with every restart of Home Assistant.
QMAX_CLEAR_REPEAT_S = 3600


def withdraw_qmax(client, device_topic) -> bool:
    """Clear Qmax and SoH in Home Assistant: an MQTT sensor takes the payload
    "None" as unknown (homeassistant/components/mqtt/sensor.py, PAYLOAD_NONE).
    Without it a withdrawn estimate stayed for QMAX_EXPIRE_S. False when a
    publish failed (the caller tries again)."""
    ok = [mqtt_single_out(client, f"{device_topic}/qmax_est/attributes", json.dumps({})),
          mqtt_single_out(client, f"{device_topic}/qmax_est", "None"),
          mqtt_single_out(client, f"{device_topic}/soh_est", "None")]
    return all(x is not False for x in ok)  # (False: failed; an unchanged value is not skipped within an hour)


def publish_qmax(client, device_topic, res: dict):
    """res: QmaxEstimator.result(). SoH only when a capacity is known."""
    mqtt_single_out(client, f"{device_topic}/qmax_est/attributes",
                    json.dumps({k: res.get(k) for k in QMAX_ATTRIBUTES}))
    mqtt_single_out(client, f"{device_topic}/qmax_est", round_to_n(res['qmax'], 4))
    if res.get('soh') is not None:
        mqtt_single_out(client, f"{device_topic}/soh_est", round_to_n(res['soh'], 4))


def hass_device_json(device_topic, device_info: DeviceInfo = None, display_name=None) -> dict:
    """The HA `device` block shared by all entities of one BMS."""
    return {
        "identifiers": [(device_info and device_info.sn) or device_topic],
        "manufacturer": (device_info and device_info.mnf) or None,
        "name": f"{display_name or device_info.name} ({device_topic})"
        if (display_name or (device_info and device_info.name)) else device_topic,
        "model": (device_info and device_info.model) or None,
        "sw_version": (device_info and device_info.sw_version) or None,
        "hw_version": (device_info and device_info.hw_version) or None,
    }


def publish_hass_discovery(client, device_topic, expire_after_seconds: int, sample: BmsSample, num_cells,
                           temperatures,
                           device_info: DeviceInfo = None, set_soc=False, cell_resistance=False,
                           pack_temp_est=False, soh_est=False, display_name=None, temperature_names=None):
    discovery_msg = {}

    # HA discovery node_id must match [a-zA-Z0-9_-] (no slashes), so flatten
    # any '/' in the alias. State topics below keep the original slashes.
    node_id = device_topic.replace('/', '_')

    device_json = hass_device_json(device_topic, device_info, display_name)

    if device_info:
        for key, value in device_info.values.items():
            if not is_none_or_nan(value):
                mqtt_single_out(client, f"{device_topic}/{key}", value)

    def _hass_discovery(k, device_class, unit, state_class=None, icon=None, name=None, long_expiry=False,
                        precision=None, expire_after=None, json_attributes_topic=None, entity_category=None):
        dm = {
            "unique_id": f"{device_topic}__{k.replace('/', '_')}",
            "name": name or capitalize_words(k.replace('/', ' ')),
            "device_class": device_class or None,
            "state_class": state_class or None,
            "unit_of_measurement": unit,
            "native_unit_of_measurement": unit,
            "suggested_unit_of_measurement": unit,
            "suggested_display_precision": precision,
            # "json_attributes_topic": f"{device_topic}/{k}",
            "state_topic": f"{device_topic}/{k}",
            "expire_after": expire_after or (max(expire_after_seconds, 3600 * 2) if long_expiry else expire_after_seconds),
            "device": device_json,
            "json_attributes_topic": json_attributes_topic,
            "entity_category": entity_category,
        }
        if icon:
            dm['icon'] = 'mdi:' + icon
        remove_none_values(dm)
        remove_none_values(dm['device'])
        discovery_msg[f"homeassistant/sensor/{node_id}/_{k.replace('/', '_')}/config"] = dm

    for k, d in sample_desc.items():
        if not is_none_or_nan(getattr(sample, d["field"])):
            _hass_discovery(k, d["device_class"],
                            state_class=d["state_class"],
                            unit=d["unit_of_measurement"],
                            icon=d.get('icon', None),
                            name=capitalize_words(d["field"]),
                            precision=d.get("precision", None)
                            )

    def _extra_discovery(key, value, desc):
        if is_none_or_nan(value):
            return
        _hass_discovery(
            key,
            desc.get("device_class"),
            desc.get("unit", ""),
            state_class=desc.get("state_class"),
            icon=desc.get("icon"),
            name=desc.get("name", capitalize_words(key.split('/')[-1].replace('_', ' '))),
            long_expiry=desc.get("long_expiry", True),
            precision=desc.get("precision"),
            entity_category=desc.get("entity_category"),
        )

    for key, value in sample.extra_values.items():
        _extra_discovery(key, value, sample.extra_desc.get(key, {}))
    if device_info:
        for key, value in device_info.values.items():
            _extra_discovery(key, value, device_info.value_desc.get(key, {}))

    for i in range(0, num_cells):
        k = 'cell_voltages/%d' % (i + 1)
        n = 'Cell Volt %0*d' % (1 + int(math.log10(num_cells)), i + 1)
        _hass_discovery(k, "voltage", state_class="measurement", name=n, unit="V", precision=3)

    if num_cells > 1:
        statistic_fields = ["min", "max", "average", "median", "delta"]
        for f in statistic_fields:
            k = 'cell_voltages/%s' % f
            _hass_discovery(k, name="Cell Volt %s" % f, device_class="voltage", state_class="measurement",
                            unit="V", precision=3)

        for f in ["min_index", "max_index"]:
            k = 'cell_voltages/%s' % f
            _hass_discovery(k, name="Cell Index %s" % f[:3], device_class=None, unit="")

    for i in range(0, len(temperatures or [])):
        k = 'temperatures/%d' % (i + 1)
        if not is_none_or_nan(temperatures[i]):
            custom_name = temperature_names[i] if i < len(temperature_names or []) else None
            _hass_discovery(k, "temperature", state_class="measurement", unit="°C", precision=1,
                            name=custom_name)

    for name, m in meter_desc.items():
        _hass_discovery('meter/%s' % name, **m, long_expiry=True, precision=2)

    if cell_resistance:
        # experimental, see bmslib/impedance.py: median per-cell resistance [mOhm]
        _hass_discovery('cell_resistance', None, "mΩ", state_class="measurement", icon="resistor",
                        name="Cell Resistance", precision=2, expire_after=CELL_RESISTANCE_EXPIRE_S)

    if soh_est:
        # experimental, see bmslib/qmax.py: capacity of the limiting cell [Ah], and
        # that against the design capacity (only published when one is known)
        _hass_discovery('qmax_est', None, "Ah", state_class="measurement", icon="battery-heart-variant",
                        name="Qmax (est.)", precision=1, expire_after=QMAX_EXPIRE_S,
                        json_attributes_topic=f"{device_topic}/qmax_est/attributes")
        _hass_discovery('soh_est', None, "%", state_class="measurement", icon="battery-heart-variant",
                        name="SoH (est.)", precision=1, expire_after=QMAX_EXPIRE_S)

    if pack_temp_est:
        # pack_temp_estimator: RC estimate of the cell temperature from MOS + ambient
        # (bmslib/pack_temp_publisher.py publishes the state on this topic)
        _hass_discovery('pack_temp_est', "temperature", "°C", state_class="measurement",
                        name="Pack Temp (RC est.)", precision=1)

    if sample.problem is not None:
        discovery_msg[f"homeassistant/binary_sensor/{node_id}/problem/config"] = {
            "unique_id": f"{device_topic}__problem",
            "name": "problem",
            "device_class": "problem",
            "entity_category": "diagnostic",
            "state_topic": f"{device_topic}/problem",
            "expire_after": expire_after_seconds,
            "device": device_json,
        }
    if sample.problem_code is not None:
        discovery_msg[f"homeassistant/sensor/{node_id}/problem_code/config"] = {
            "unique_id": f"{device_topic}__problem_code",
            "name": "problem code",
            "entity_category": "diagnostic",
            "state_topic": f"{device_topic}/problem_code",
            "expire_after": expire_after_seconds,
            "device": device_json,
            "icon": "mdi:alert-circle-outline",
        }

    if sample.alarms:
        for alarm_name in sample.alarms:
            discovery_msg[f"homeassistant/binary_sensor/{node_id}/alarm_{alarm_name}/config"] = {
                "unique_id": f"{device_topic}__alarm_{alarm_name}",
                "name": alarm_name.upper(),
                "device_class": "problem",
                "entity_category": "diagnostic",
                "state_topic": f"{device_topic}/alarm/{alarm_name}",
                "expire_after": expire_after_seconds,
                "device": device_json,
            }

    if sample.balancing_cells is not None:
        discovery_msg[f"homeassistant/binary_sensor/{node_id}/balancing/config"] = {
            "unique_id": f"{device_topic}__balancing",
            "name": "balancing",
            "entity_category": "diagnostic",
            "state_topic": f"{device_topic}/balancing",
            "expire_after": expire_after_seconds,
            "device": device_json,
            "icon": "mdi:scale-balance",
        }
        discovery_msg[f"homeassistant/sensor/{node_id}/balancing_cells/config"] = {
            "unique_id": f"{device_topic}__balancing_cells",
            "name": "balancing cells",
            "entity_category": "diagnostic",
            "state_topic": f"{device_topic}/balancing_cells",
            "expire_after": expire_after_seconds,
            "device": device_json,
            "icon": "mdi:scale-balance",
        }

    if sample.battery_charging is not None:
        discovery_msg[f"homeassistant/binary_sensor/{node_id}/battery_charging/config"] = {
            "unique_id": f"{device_topic}__battery_charging",
            "name": "battery charging",
            "device_class": "battery_charging",
            "state_topic": f"{device_topic}/battery_charging",
            "expire_after": expire_after_seconds,
            "device": device_json,
        }
    if sample.battery_mode is not None:
        discovery_msg[f"homeassistant/sensor/{node_id}/battery_mode/config"] = {
            "unique_id": f"{device_topic}__battery_mode",
            "name": "battery mode",
            "device_class": "enum",
            "options": ["UNKNOWN", "BULK", "ABSORPTION", "FLOAT"],
            "state_topic": f"{device_topic}/battery_mode",
            "expire_after": expire_after_seconds,
            "device": device_json,
            "icon": "mdi:battery-charging-medium",
        }

    switches = (sample.switches and sample.switches.keys())
    if switches:
        for switch_name in switches:
            discovery_msg[f"homeassistant/switch/{node_id}/{switch_name}/config"] = {
                "unique_id": f"{device_topic}__switch_{switch_name}",
                "name": f"{switch_name}",
                "device_class": 'outlet',
                # "json_attributes_topic": f"{device_topic}/{switch_name}",
                "state_topic": f"{device_topic}/switch/{switch_name}",
                "expire_after": expire_after_seconds,
                "device": device_json,
                "command_topic": f"homeassistant/switch/{node_id}/{switch_name}/set",
            }

            discovery_msg[f"homeassistant/binary_sensor/{node_id}/{switch_name}/config"] = {
                "unique_id": f"{device_topic}__switch_{switch_name}",
                "name": f"{switch_name} switch",
                "device_class": 'power',
                # "json_attributes_topic": f"{device_topic}/{switch_name}",
                "expire_after": expire_after_seconds,
                "device": device_json,
                "state_topic": f"{device_topic}/switch/{switch_name}",
                "command_topic": f"homeassistant/switch/{node_id}/{switch_name}/set",
            }

    if set_soc:
        # HA `number` that writes the BMS' SOC gauge (Daly, #144). State follows the reported SOC.
        discovery_msg[f"homeassistant/number/{node_id}/set_soc/config"] = {
            "unique_id": f"{device_topic}__set_soc",
            "name": "set SOC",
            "entity_category": "config",
            "icon": "mdi:battery-sync",
            "unit_of_measurement": "%",
            "min": 0, "max": 100, "step": 1, "mode": "box",
            "state_topic": f"{device_topic}/soc/soc_percent",
            "command_topic": f"homeassistant/number/{node_id}/set_soc/set",
            "device": device_json,
        }

    for topic, data in discovery_msg.items():
        j = json.dumps(data)
        logger.debug('discovery msg %s: %s', topic, j)
        mqtt_single_out(client, topic, j)


# ------------------------------------------------------------------ JK settings (jk_settings option)

# Settings states come with the settings refresh (JkSettingsService.refresh_period,
# 5 min) and after a change, not with every sample: the read-only sensors need a
# longer expiry than the sample values.
SETTINGS_EXPIRE_MIN_S = 2 * 3600
# HA rejects sensor states longer than 255 characters
_HA_STATE_MAX_LEN = 255
# settings states are sent again this long after a settings discovery (see resend_after_discovery)
SETTINGS_RESEND_AFTER_DISCOVERY_S = 2.0


def _settings_command_topic(node_id, name):
    component = 'switch' if SETTINGS[name].kind == 'switch' else 'number'
    return f"homeassistant/{component}/{node_id}/{name}/set"


def _readable_settings(exclude=()):
    return [s for s in SETTINGS.values() if s.read is not None and s.name not in exclude]


def publish_jk_settings_discovery(client, device_topic, device_json, writable, expire_after_seconds: int,
                                  exclude=(), removals=True):
    """HA entities for the JK settings that can be read from the settings frame: writable
    ones as config `number`/`switch`, the rest as diagnostic `sensor`/`binary_sensor`;
    plus the `settings_last_change` sensor. `exclude`: names to leave out (JK-PB float
    switch, already exposed as the `float_charge` sample switch). `removals`: also send the
    empty configs that remove each setting's entity under the other component."""
    node_id = device_topic.replace('/', '_')
    device_json = dict(device_json)
    remove_none_values(device_json)
    discovery_msg = {}
    remove_msg = {}

    for s in _readable_settings(exclude):
        is_switch = s.kind == 'switch'
        dm = {
            "unique_id": f"{device_topic}__setting_{s.name}",
            "name": LABELS.get(s.name, s.name),
            "state_topic": f"{device_topic}/settings/{s.name}",
            "device": device_json,
        }
        if is_switch:
            dm.update(payload_on="ON", payload_off="OFF")
        else:
            dm["unit_of_measurement"] = s.unit
        writable_components = ('switch', 'number')
        read_only_components = ('binary_sensor', 'sensor')
        if s.name in writable:
            component = writable_components[0 if is_switch else 1]
            other = read_only_components[0 if is_switch else 1]
            dm["entity_category"] = "config"
            dm["command_topic"] = _settings_command_topic(node_id, s.name)
            if not is_switch:
                dm.update(mode="box", min=s.min, max=s.max, step=s.step)
        else:
            component = read_only_components[0 if is_switch else 1]
            other = writable_components[0 if is_switch else 1]
            dm["entity_category"] = "diagnostic"
            dm["expire_after"] = max(expire_after_seconds, SETTINGS_EXPIRE_MIN_S)
        remove_none_values(dm)
        # same unique_id under the other component when `settings_write` changed: an empty config
        # makes HA remove that (now stale) entity; sent before the new config
        remove_msg[f"homeassistant/{other}/{node_id}/setting_{s.name}/config"] = ''
        discovery_msg[f"homeassistant/{component}/{node_id}/setting_{s.name}/config"] = dm

    discovery_msg[f"homeassistant/sensor/{node_id}/settings_last_change/config"] = {
        "unique_id": f"{device_topic}__settings_last_change",
        "name": "Settings last change",
        "entity_category": "diagnostic",
        "icon": "mdi:cog-transfer",
        "state_topic": f"{device_topic}/settings/last_change",
        "device": device_json,
    }

    for topic, data in (remove_msg.items() if removals else ()):
        mqtt_single_out(client, topic, data)
    for topic, data in discovery_msg.items():
        mqtt_single_out(client, topic, json.dumps(data))


class MqttSettingsPublisher:
    """The JkSettingsService publish protocol (states, last_change) on MQTT.

    With `writable` set, it also owns the settings HA discovery: sent before the first states and
    again whenever the entity set (`exclude`, `writable`) changed, so HA never gets states for
    entities it does not know yet (they would show "unavailable" until the next refresh).
    `device_json`: callable returning the HA device block (default: from the device topic)."""

    def __init__(self, client, device_topic, exclude=(), writable=None, expire_after_seconds=0, device_json=None):
        self.client = client
        self.device_topic = device_topic
        self.exclude = frozenset(exclude)
        self.writable = None if writable is None else frozenset(writable)
        self.expire_after_seconds = expire_after_seconds
        self.device_json = device_json
        self._discovered = None  # (exclude, writable) of the last discovery
        self._force_states = False  # next states() goes past the dedup (after a discovery)
        self._resend_at = None  # time.time() for the one-time re-send after a discovery

    def discovery(self):
        """Publish the settings discovery now; the next states() is forced."""
        if self.writable is None:
            return
        dj = self.device_json() if self.device_json else hass_device_json(self.device_topic)
        entity_set = (frozenset(self.exclude), self.writable)
        # removals only when the entity set is new or changed, not with every periodic discovery
        publish_jk_settings_discovery(self.client, self.device_topic, dj, self.writable,
                                      self.expire_after_seconds, exclude=self.exclude,
                                      removals=self._discovered != entity_set)
        self._discovered = entity_set
        self._force_states = True
        self._resend_at = time.time() + SETTINGS_RESEND_AFTER_DISCOVERY_S

    def resend_after_discovery(self, settings: dict):
        """Called every sample: states sent right behind the configs can reach the broker before
        HA subscribed to the new entities' state topics (not retained, so lost); send them once
        more a moment after each discovery."""
        if self._resend_at is None or not settings or time.time() < self._resend_at:
            return
        self._resend_at = None
        self.states(settings, force=True)

    def _ensure_discovery(self):
        if self.writable is not None and self._discovered != (frozenset(self.exclude), self.writable):
            self.discovery()

    def states(self, settings: dict, force=False):
        """Numbers plain (2.65), switches ON/OFF, not retained. Unchanged values are
        deduplicated like other states unless `force` or right after a discovery."""
        self._ensure_discovery()
        if self._force_states:
            force, self._force_states = True, False
        for name, value in (settings or {}).items():
            if name in self.exclude or value is None:
                continue
            topic = f"{self.device_topic}/settings/{name}"
            if force:
                _last_values.pop(topic, None)
            mqtt_single_out(self.client, topic, format_value(name, value))

    def last_change(self, text: str):
        # always published: the same text twice (e.g. a repeated rejection) is two events
        self._ensure_discovery()  # the last-change sensor is part of the settings discovery
        topic = f"{self.device_topic}/settings/last_change"
        text = str(text)
        if len(text) > _HA_STATE_MAX_LEN:
            text = text[:_HA_STATE_MAX_LEN - 3] + '...'
        _last_values.pop(topic, None)
        mqtt_single_out(self.client, topic, text)


_switch_callbacks = {}
_message_queue = queue.Queue()

# Topics we only READ (e.g. ambient temperatures for the pack-temp estimator).
# Unlike _switch_callbacks these are plain functions, called directly on the
# paho network thread: the callee must be thread-safe and must not block.
_state_callbacks = {}

# Command topics handled by a plain function on the paho network thread (JK
# settings: JkSettingsService.enqueue is thread-safe and does not block). Like
# _switch_callbacks they are re-subscribed by resubscribe_commands().
_command_callbacks = {}
_retained_warned = set()  # command topics we already warned about a retained message on


def register_state_topic(topic: str, callback):
    """Route messages on `topic` to `callback(payload: str)`. Takes effect for the
    broker at the next subscribe_state_topics() -- call that from on_connect, so
    the subscription survives a broker restart (clean session). Exact topics
    only: dispatch is a dict lookup on the message topic."""
    if '+' in topic or '#' in topic:
        raise ValueError('wildcards are not supported in a state topic: %r' % topic)
    _state_callbacks[topic] = callback


def subscribe_state_topics(mqtt_client: paho.Client):
    for topic in _state_callbacks:
        logger.debug("subscribe %s", topic)
        mqtt_client.subscribe(topic, qos=0)


async def mqtt_process_action_queue():
    while not _message_queue.empty():
        callback, arg = _message_queue.get(block=False)
        try:
            await callback(arg)
        except Exception as e:
            logger.error('exception in action callback: %s', e)
            logger.error('Stack: %s', traceback.format_exc())
            await asyncio.sleep(1)


def subscribe_switches(mqtt_client: paho.Client, device_topic, bms: BtBms, switches):
    async def set_switch(switch_name: str, state: bool):
        assert isinstance(state, bool)
        logger.info('Set %s %s switch %s', bms.name, switch_name, state)
        await bms.set_switch(switch_name, state)
        topic = f"{device_topic}/switch/{switch_name}"
        mqtt_single_out(mqtt_client, topic, 'ON' if state else 'OFF')

    node_id = device_topic.replace('/', '_')
    for switch_name in switches:
        state_topic = f"homeassistant/switch/{node_id}/{switch_name}/set"
        logger.debug("subscribe %s", state_topic)
        mqtt_client.subscribe(state_topic, qos=2)
        _switch_callbacks[state_topic] = \
            lambda msg, sn=switch_name: set_switch(sn, msg.lower() == "on")


def subscribe_set_soc(mqtt_client: paho.Client, device_topic, bms: BtBms):
    async def set_soc(payload: str):
        soc = float(payload)
        logger.info('Set %s SOC gauge to %.1f%%', bms.name, soc)
        await bms.set_soc(soc)

    node_id = device_topic.replace('/', '_')
    topic = f"homeassistant/number/{node_id}/set_soc/set"
    logger.debug("subscribe %s", topic)
    mqtt_client.subscribe(topic, qos=2)
    _switch_callbacks[topic] = set_soc


def subscribe_jk_settings(mqtt_client: paho.Client, device_topic, service, exclude=()):
    """Route the command topics of the writable settings to `service.enqueue(name, payload)`."""
    node_id = device_topic.replace('/', '_')
    for s in _readable_settings(exclude):
        if s.name not in service.writable:
            continue
        topic = _settings_command_topic(node_id, s.name)
        _command_callbacks[topic] = lambda payload, n=s.name: service.enqueue(n, payload)
        logger.debug("subscribe %s", topic)
        mqtt_client.subscribe(topic, qos=2)


def resubscribe_commands(mqtt_client: paho.Client):
    """Subscribe every registered command topic again (switches, set SOC, JK settings).
    Call from on_connect: after a broker restart (clean session) the subscriptions
    made on the first sample are gone."""
    # dict.copy() is atomic under the GIL; iterating the live dicts on the paho thread raced
    # with the first sample filling them (RuntimeError: dictionary changed size)
    for topic in list(_switch_callbacks.copy()) + list(_command_callbacks.copy()):
        logger.debug("re-subscribe %s", topic)
        mqtt_client.subscribe(topic, qos=2)


def mqtt_message_handler(client, userdata, message: paho.MQTTMessage):
    # runs on paho's network thread: an exception here would end that thread,
    # and a state topic carries whatever the user pointed it at
    try:
        payload = message.payload.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning('ignoring a non-UTF-8 message on %s', message.topic)
        return
    state_cb = _state_callbacks.get(message.topic)
    if state_cb is not None:
        logger.debug("received state %s: %s", message.topic, payload)
        try:
            state_cb(payload)
        except Exception as e:
            logger.warning('state callback for %s failed: %s', message.topic, e)
        return
    logger.info("received msg %s: %s", message.topic, payload)
    command_cb = _command_callbacks.get(message.topic)
    if command_cb is not None:
        if getattr(message, 'retain', False):
            # a retained command is delivered again on every (re)subscribe and would undo
            # whatever was changed since (e.g. in the JK app): settings writes must be live
            if message.topic not in _retained_warned:
                _retained_warned.add(message.topic)
                logger.warning('ignoring retained settings command on %s (payload %s); publish settings '
                               'commands without retain and clear the retained message', message.topic, payload)
            return
        try:
            command_cb(payload)
        except Exception as e:
            logger.error('command callback for %s failed: %s', message.topic, e)
        return
    callback = _switch_callbacks.get(message.topic, None)
    if callback:
        _message_queue.put((callback, payload))
    else:
        logger.warning("No callback for topic %s (payload %s)", message.topic, payload)


def paho_monkey_patch():
    def _handle_pingresp(self):
        if self._in_packet['remaining_length'] != 0:
            return paho.MQTT_ERR_PROTOCOL

        # No longer waiting for a PINGRESP.
        # self._ping_t = 0
        self._easy_log(paho.MQTT_LOG_DEBUG, "Received PINGRESP (patched)")
        return paho.MQTT_ERR_SUCCESS

    paho.Client._handle_pingresp = _handle_pingresp

    logger.debug("applied paho monkey patch _handle_pingresp")
