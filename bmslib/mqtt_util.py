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


def publish_hass_discovery(client, device_topic, expire_after_seconds: int, sample: BmsSample, num_cells,
                           temperatures,
                           device_info: DeviceInfo = None, set_soc=False):
    discovery_msg = {}

    # HA discovery node_id must match [a-zA-Z0-9_-] (no slashes), so flatten
    # any '/' in the alias. State topics below keep the original slashes.
    node_id = device_topic.replace('/', '_')

    device_json = {
        "identifiers": [(device_info and device_info.sn) or device_topic],
        "manufacturer": (device_info and device_info.mnf) or None,
        "name": f"{device_info.name} ({device_topic})" if (device_info and device_info.name) else device_topic,
        "model": (device_info and device_info.model) or None,
        "sw_version": (device_info and device_info.sw_version) or None,
        "hw_version": (device_info and device_info.hw_version) or None,
    }

    def _hass_discovery(k, device_class, unit, state_class=None, icon=None, name=None, long_expiry=False,
                        precision=None):
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
            "expire_after": max(expire_after_seconds, 3600 * 2) if long_expiry else expire_after_seconds,
            "device": device_json,
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
            _hass_discovery(k, "temperature", state_class="measurement", unit="°C", precision=1)

    for name, m in meter_desc.items():
        _hass_discovery('meter/%s' % name, **m, long_expiry=True, precision=2)

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


_switch_callbacks = {}
_message_queue = queue.Queue()


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


def mqtt_message_handler(client, userdata, message: paho.MQTTMessage):
    payload = message.payload.decode("utf-8")
    logger.info("received msg %s: %s", message.topic, payload)
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
