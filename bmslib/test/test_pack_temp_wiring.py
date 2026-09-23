"""pack_temp_estimator wiring: config -> AmbientCache -> MQTT state topics ->
BmsSampler -> pack_temp_est, and the pack-temp tag on impedance windows.

The modules themselves are tested in test_pack_temp_rc.py and
test_pack_temp_pipeline.py; this is about how they are plugged in.
"""
import asyncio
import json
import math
import time

import paho.mqtt.client as paho
import pytest

import bmslib.mqtt_util as mqtt_util
from bmslib.ambient_cache import AmbientCache
from bmslib.bms import BmsSample
from bmslib.pack_temp_publisher import AMBIENT_MAX_AGE_DEFAULT_S, ambient_cache_from_config
from bmslib.sampling import BmsSampler


@pytest.fixture(autouse=True)
def _clean_state_callbacks(monkeypatch):
    monkeypatch.setattr(mqtt_util, '_state_callbacks', {})


class _Client:
    def __init__(self):
        self.published = {}
        self.subscribed = []

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload

        class R:
            rc = paho.MQTT_ERR_SUCCESS

        return R()

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


# ------------------------------------------------------------------ config

def test_off_by_default():
    reg = []
    assert ambient_cache_from_config({}, lambda *a: reg.append(a)) is None
    assert ambient_cache_from_config({'pack_temp_estimator': False, 'pack_temp_room_topic': 'x'},
                                     lambda *a: reg.append(a)) is None
    assert reg == []


def test_no_topics_means_empty_channels_not_defaults():
    reg = []
    cache = ambient_cache_from_config({'pack_temp_estimator': True}, lambda *a: reg.append(a))
    assert isinstance(cache, AmbientCache) and reg == []
    assert cache.get('room') is None and cache.get('outdoor') is None
    assert cache.max_age_s == AMBIENT_MAX_AGE_DEFAULT_S


def test_topics_are_registered_per_channel():
    reg = {}
    cache = ambient_cache_from_config({'pack_temp_estimator': True, 'pack_temp_room_topic': ' room/t ',
                                       'pack_temp_outdoor_topic': '', 'pack_temp_ambient_max_age': 120},
                                      lambda topic, cb: reg.__setitem__(topic, cb))
    assert list(reg) == ['room/t'] and cache.max_age_s == 120
    reg['room/t']('21.5')
    assert cache.get('room') == pytest.approx(21.5)
    assert cache.get('outdoor') is None


@pytest.mark.parametrize('bad', ['abc', -5, 0, float('nan'), float('inf')])
def test_bad_max_age_falls_back_to_the_default(bad):
    cache = ambient_cache_from_config({'pack_temp_estimator': True, 'pack_temp_ambient_max_age': bad},
                                      lambda *a: None)
    assert cache.max_age_s == AMBIENT_MAX_AGE_DEFAULT_S


# ------------------------------------------------------------------ MQTT dispatch

def test_state_topic_is_delivered_synchronously_and_resubscribed_on_connect():
    cache = ambient_cache_from_config({'pack_temp_estimator': True, 'pack_temp_room_topic': 'room/t'},
                                      mqtt_util.register_state_topic)
    client = _Client()
    mqtt_util.subscribe_state_topics(client)  # what main.py's on_connect does
    mqtt_util.subscribe_state_topics(client)  # ... again after a reconnect
    assert client.subscribed == ['room/t', 'room/t']

    mqtt_util.mqtt_message_handler(None, None, _Msg('room/t', '{"state": "19.5"}'))
    # not queued for the asyncio loop, which would await the plain function
    assert mqtt_util._message_queue.empty()
    assert cache.get('room') == pytest.approx(19.5)

    mqtt_util.mqtt_message_handler(None, None, _Msg('room/t', 'unavailable'))
    assert cache.get('room') == pytest.approx(19.5)  # unparsable keeps the last reading, ages out


def test_a_failing_state_callback_does_not_escape_the_paho_thread():
    def boom(payload):
        raise ValueError('x')

    mqtt_util.register_state_topic('a/b', boom)
    mqtt_util.mqtt_message_handler(None, None, _Msg('a/b', '1'))


# ------------------------------------------------------------------ sampler

class _Bms:
    name = 'pt_fake'
    address = 'serial'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __init__(self, mos=(40.0,)):
        self.k = 0
        self.mos = list(mos)
        self.t0 = time.time()

    def __str__(self):
        return 'FakeBms(pt)'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        mos = self.mos[min(self.k, len(self.mos) - 1)]
        self.k += 1
        return BmsSample(voltage=53.0, current=10.0, soc=60.0, mos_temperature=mos, timestamp=self.t0 + self.k)

    async def fetch_voltages(self):
        return [3300] * 16

    def debug_data(self):
        return None


def _sampler(bms, client=None, **kw):
    s = BmsSampler(bms, mqtt_client=client, dt_max_seconds=120, expire_after_seconds=60, publish_period=3600, **kw)
    s.num_samples = 1
    s._last_power = 530
    return s


def test_sampler_publishes_pack_temp_from_mos_alone_without_ambient():
    client = _Client()
    s = _sampler(_Bms(mos=(40.0,)), client, ambient_cache=AmbientCache())
    asyncio.run(s())
    # nothing but the MOS reading: the seed is the MOS value, no invented ambient
    assert float(client.published['pt_fake/pack_temp_est']) == pytest.approx(40.0)
    d = json.loads(client.published['homeassistant/sensor/pt_fake/_pack_temp_est/config'])
    assert d['state_topic'] == 'pt_fake/pack_temp_est'
    assert d['device_class'] == 'temperature' and d['unit_of_measurement'] == '°C'
    assert d['unique_id'] == 'pt_fake__pack_temp_est'
    assert d['device']['identifiers'] == ['pt_fake']


def test_sampler_uses_ambient_when_present():
    cache = AmbientCache()
    cache.topic_callback('room')('20.0')
    client = _Client()
    s = _sampler(_Bms(mos=(40.0,)), client, ambient_cache=cache)
    asyncio.run(s())
    # conductance-weighted seed: (40*0.0017 + 20*0.0006) / 0.0023
    assert float(client.published['pt_fake/pack_temp_est']) == pytest.approx((40 * 17 + 20 * 6) / 23, abs=0.01)


def test_no_pack_temp_without_the_option_or_for_groups():
    assert _sampler(_Bms())._pack_temp_publisher is None

    class _Group(_Bms):
        is_virtual = True

    assert _sampler(_Group(), ambient_cache=AmbientCache())._pack_temp_publisher is None


def test_impedance_rows_carry_the_pack_temp_tag_or_none():
    s = _sampler(_Bms(mos=(40.0, 40.0, math.nan)), ambient_cache=AmbientCache(), impedance_estimator=True)
    for _ in range(3):
        asyncio.run(s())
    tags = [r[5] for r in s.impedance._rows]
    assert tags[0] == pytest.approx(40.0) and tags[1] == pytest.approx(40.0)
    assert tags[2] is None  # no MOS reading this iteration: unknown, not the last value


def test_impedance_without_pack_temp_has_no_tag():
    s = _sampler(_Bms(), impedance_estimator=True)
    asyncio.run(s())
    assert s.impedance._rows[-1][5] is None
