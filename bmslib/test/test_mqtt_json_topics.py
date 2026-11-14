"""mqtt_json_topics (#204): one JSON object per group under <device>/json/, next to
the per-metric topics, which stay exactly as they are."""
import json
import logging
import math
from types import SimpleNamespace

import paho.mqtt.client as paho
import pytest

import bmslib.mqtt_util as mqtt_util
from bmslib.bms import BmsSample
from bmslib.mqtt_util import publish_cell_voltages, publish_sample, publish_temperatures
from bmslib.pwmath import Integrator
from bmslib.sampling import BmsSampler


class _Client:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload
        return SimpleNamespace(rc=paho.MQTT_ERR_SUCCESS)

    def json(self, topic):
        return json.loads(self.published[topic])


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(mqtt_util, '_last_values', {})
    monkeypatch.setattr(mqtt_util, '_json_warned', set())
    monkeypatch.setattr(mqtt_util, 'json_topics', False)


def _sample(**kw):
    return BmsSample(voltage=53.21, current=-4.5, soc=81, switches={'charge': True, 'discharge': False},
                     alarms={'cell_overvoltage': False}, problem=False, battery_mode='float', **kw)


def _publish_all(c):
    publish_sample(c, 'bat', _sample(extra_values={'heater': 'off'}))
    publish_cell_voltages(c, 'bat', [3301, 3305, 3299, 3310])
    publish_temperatures(c, 'bat', [21.5, math.nan, 23.04])


def test_off_by_default_publishes_no_json_and_the_per_metric_topics_are_unchanged():
    off = _Client()
    _publish_all(off)
    assert not [t for t in off.published if '/json/' in t]

    mqtt_util._last_values.clear()
    mqtt_util.set_json_topics(True)
    on = _Client()
    _publish_all(on)
    assert {t: p for t, p in on.published.items() if '/json/' not in t} == off.published


def test_sample_object_mirrors_the_topics():
    mqtt_util.set_json_topics(True)
    c = _Client()
    publish_sample(c, 'bat', _sample(extra_values={'heater': 'off'}))
    j = c.json('bat/json/sample')
    assert j['soc/total_voltage'] == 53.21 and j['soc/current'] == -4.5 and j['soc/soc_percent'] == 81
    assert j['switch'] == {'charge': True, 'discharge': False}
    assert j['alarm'] == {'cell_overvoltage': False}
    assert j['problem'] is False and j['battery_mode'] == 'float' and j['heater'] == 'off'
    # every scalar key is a per-metric topic carrying the same value
    for k, v in j.items():
        if not isinstance(v, (dict, bool)):
            assert mqtt_util._json_num(c.published[f'bat/{k}']) == v, k  # topics carry '53.21', JSON 53.21


def test_cell_voltages_and_temperatures():
    mqtt_util.set_json_topics(True)
    c = _Client()
    publish_cell_voltages(c, 'bat', [3301, 3305, 3299, 3310])
    publish_temperatures(c, 'bat', [21.5, math.nan, 23.04])
    cv = c.json('bat/json/cell_voltages')
    assert cv['cells'] == [3.301, 3.305, 3.299, 3.31]
    assert cv['min'] == 3.299 and cv['min_index'] == 3 and cv['max'] == 3.31 and cv['max_index'] == 4  # 1-based
    assert math.isclose(cv['delta'], 0.011)
    # a missing sensor keeps its position as null, never NaN (invalid JSON)
    assert c.json('bat/json/temperatures') == [21.5, None, 23.04]


def test_one_cell_has_no_stats_but_still_its_voltage():
    mqtt_util.set_json_topics(True)
    c = _Client()
    publish_cell_voltages(c, 'bat', [3301])
    assert c.json('bat/json/cell_voltages') == {'cells': [3.301]}


def test_an_unserialisable_value_drops_only_the_json_message(caplog):
    mqtt_util.set_json_topics(True)
    c = _Client()
    with caplog.at_level(logging.WARNING):
        publish_sample(c, 'bat', _sample(extra_values={'blob': object()}))
        mqtt_util._last_values.clear()
        publish_sample(c, 'bat', _sample(extra_values={'blob': object()}))
    assert 'bat/json/sample' not in c.published
    assert c.published['bat/soc/total_voltage'] == '53.21'  # the sample itself still went out
    assert sum('bat/json/sample not published' in r.getMessage() for r in caplog.records) == 1  # warned once


def test_meters():
    meters = [Integrator('total_energy', 60, 12.3456), Integrator('total_charge', 60, math.nan)]
    c = _Client()
    stub = SimpleNamespace(mqtt_topic_prefix='bat', meters=meters, mqtt_client=c, sinks=[])

    BmsSampler.publish_meters(stub)
    assert 'bat/json/meter' not in c.published

    mqtt_util._last_values.clear()
    mqtt_util.set_json_topics(True)
    BmsSampler.publish_meters(stub)
    assert c.json('bat/json/meter') == {'total_energy': 12.346}  # NaN meter left out, as on its topic
    assert c.published['bat/meter/total_energy'] == 12.346
