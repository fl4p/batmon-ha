"""Home Assistant MQTT discovery metadata."""
import json

import paho.mqtt.client as paho

from bmslib.bms import BmsSample
from bmslib.mqtt_util import publish_hass_discovery, publish_sample


def test_count_sensors_have_no_unit_of_measurement():
    published = {}

    class Result:
        rc = paho.MQTT_ERR_SUCCESS

    class Client:
        def publish(self, topic, payload, retain=False):
            published[topic] = json.loads(payload)
            return Result()

    sample = BmsSample(voltage=13.2, current=0, num_cycles=81)
    publish_hass_discovery(Client(), "test/count-units", 300, sample, 4, [])

    node = "test_count-units"
    count_topics = (
        f"homeassistant/sensor/{node}/_soc_num_cycles/config",
        f"homeassistant/sensor/{node}/_meter_sample_count/config",
        f"homeassistant/sensor/{node}/_meter_total_cycles/config",
    )
    for topic in count_topics:
        assert "unit_of_measurement" not in published[topic]
        assert "native_unit_of_measurement" not in published[topic]
        assert "suggested_unit_of_measurement" not in published[topic]


def test_alarm_states_are_published_as_read_only_binary_sensors():
    discovery = {}
    states = {}

    class Result:
        rc = paho.MQTT_ERR_SUCCESS

    class DiscoveryClient:
        def publish(self, topic, payload, retain=False):
            discovery[topic] = json.loads(payload)
            return Result()

    class StateClient:
        def publish(self, topic, payload, retain=False):
            states[topic] = payload
            return Result()

    sample = BmsSample(
        voltage=13.2,
        current=0,
        alarms={"hv": False, "ocd": True},
    )
    publish_sample(StateClient(), "test/alarms", sample)
    publish_hass_discovery(DiscoveryClient(), "test/alarms", 300, sample, 4, [])

    assert states["test/alarms/alarm/hv"] == "OFF"
    assert states["test/alarms/alarm/ocd"] == "ON"

    topic = "homeassistant/binary_sensor/test_alarms/alarm_ocd/config"
    assert discovery[topic]["name"] == "OCD"
    assert discovery[topic]["device_class"] == "problem"
    assert discovery[topic]["entity_category"] == "diagnostic"
    assert discovery[topic]["state_topic"] == "test/alarms/alarm/ocd"
    assert "command_topic" not in discovery[topic]


# ------------------------------------------------------------------ JK settings

import asyncio  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

import bmslib.mqtt_util as mqtt_util  # noqa: E402
from bmslib.jk_settings_service import JkSettingsService  # noqa: E402
from bmslib.models.jk_settings import SETTINGS  # noqa: E402
from bmslib.mqtt_util import MqttSettingsPublisher, hass_device_json, publish_jk_settings_discovery  # noqa: E402
from bmslib.sampling import BmsSampler  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_mqtt_state(monkeypatch):
    monkeypatch.setattr(mqtt_util, '_last_values', {})
    monkeypatch.setattr(mqtt_util, '_switch_callbacks', {})
    monkeypatch.setattr(mqtt_util, '_command_callbacks', {})


class _Recorder:
    def __init__(self):
        self.published = {}
        self.log = []  # (topic, payload, retain)
        self.subscribed = []

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload
        self.log.append((topic, payload, retain))

        class R:
            rc = paho.MQTT_ERR_SUCCESS

        return R()

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def config(self, topic):
        return json.loads(self.published[topic])


DEV = "test/jk"
NODE = "test_jk"


def _settings_discovery(writable=(), exclude=()):
    c = _Recorder()
    publish_jk_settings_discovery(c, DEV, hass_device_json(DEV, None), frozenset(writable), 30, exclude=exclude)
    return c


def test_writable_setting_is_a_config_number():
    c = _settings_discovery(writable={'charge_utp'})
    d = c.config(f"homeassistant/number/{NODE}/setting_charge_utp/config")
    assert d['unique_id'] == f"{DEV}__setting_charge_utp"
    assert d['name'] == "Charge UTP"
    assert d['entity_category'] == 'config'
    assert d['mode'] == 'box'
    s = SETTINGS['charge_utp']
    assert (d['min'], d['max'], d['step']) == (s.min, s.max, s.step)
    assert d['unit_of_measurement'] == '°C'
    assert d['state_topic'] == f"{DEV}/settings/charge_utp"
    assert d['command_topic'] == f"homeassistant/number/{NODE}/charge_utp/set"
    assert d['device']['identifiers'] == [DEV]
    # not writable -> no number entity (only the empty config that removes a stale one)
    assert c.published[f"homeassistant/number/{NODE}/setting_cell_ovp/config"] == ''


def test_read_only_setting_is_a_diagnostic_sensor():
    c = _settings_discovery(writable={'charge_utp'})
    d = c.config(f"homeassistant/sensor/{NODE}/setting_cell_count/config")
    assert d['entity_category'] == 'diagnostic'
    assert d['name'] == 'Cell count'
    assert d['state_topic'] == f"{DEV}/settings/cell_count"
    assert 'command_topic' not in d
    assert 'unit_of_measurement' not in d  # unit None
    assert d['expire_after'] >= 3600  # states come with the settings refresh, not every sample
    d = c.config(f"homeassistant/sensor/{NODE}/setting_cell_ovp/config")
    assert d['name'] == 'Cell OVP' and d['unit_of_measurement'] == 'V'


def test_writable_switch_setting_and_read_only_switch():
    c = _settings_discovery(writable={'display_always_on'})
    d = c.config(f"homeassistant/switch/{NODE}/setting_display_always_on/config")
    assert d['entity_category'] == 'config'
    assert d['payload_on'] == 'ON' and d['payload_off'] == 'OFF'
    assert d['state_topic'] == f"{DEV}/settings/display_always_on"
    assert d['command_topic'] == f"homeassistant/switch/{NODE}/display_always_on/set"
    d = c.config(f"homeassistant/binary_sensor/{NODE}/setting_heating/config")
    assert d['entity_category'] == 'diagnostic'
    assert d['payload_on'] == 'ON' and d['payload_off'] == 'OFF'
    assert 'command_topic' not in d


def test_entity_of_the_other_component_is_removed():
    """Same unique_id as number/switch when writable, sensor/binary_sensor when read-only: after
    `settings_write` changed, an empty config removes the entity of the other component in HA."""
    c = _settings_discovery(writable={'charge_utp', 'display_always_on'})
    assert c.published[f"homeassistant/sensor/{NODE}/setting_charge_utp/config"] == ''
    assert c.published[f"homeassistant/number/{NODE}/setting_cell_ovp/config"] == ''
    assert c.published[f"homeassistant/binary_sensor/{NODE}/setting_display_always_on/config"] == ''
    assert c.published[f"homeassistant/switch/{NODE}/setting_heating/config"] == ''
    # the live ones are real configs
    assert c.config(f"homeassistant/number/{NODE}/setting_charge_utp/config")['command_topic']
    assert c.config(f"homeassistant/sensor/{NODE}/setting_cell_ovp/config")['entity_category'] == 'diagnostic'
    # removal before the new config with the same unique_id
    topics = [t for t, _, _ in c.log]
    assert topics.index(f"homeassistant/sensor/{NODE}/setting_charge_utp/config") < \
        topics.index(f"homeassistant/number/{NODE}/setting_charge_utp/config")
    assert all(retain is False for _, _, retain in c.log)  # like all discovery here


def test_only_readable_settings_are_discovered():
    c = _settings_discovery()
    names = {json.loads(p)['unique_id'].split('__setting_')[1]
             for t, p in c.published.items() if p and '__setting_' in json.loads(p).get('unique_id', '')}
    assert names == {n for n, s in SETTINGS.items() if s.read is not None}


def test_settings_last_change_sensor():
    c = _settings_discovery()
    d = c.config(f"homeassistant/sensor/{NODE}/settings_last_change/config")
    assert d['entity_category'] == 'diagnostic'
    assert d['icon'] == 'mdi:cog-transfer'
    assert d['state_topic'] == f"{DEV}/settings/last_change"
    assert d['unique_id'] == f"{DEV}__settings_last_change"


def test_discovery_exclude():
    c = _settings_discovery(writable={'charging_float_mode'}, exclude={'charging_float_mode'})
    assert not [t for t in c.published if 'charging_float_mode' in t]


def test_settings_states_published_plain_and_not_retained():
    c = _Recorder()
    pub = MqttSettingsPublisher(c, DEV)
    pub.states({'cell_ovp': 3.65, 'charge_utp': -5.0, 'cell_count': 8, 'display_always_on': True,
                'heating': False})
    assert c.published[f"{DEV}/settings/cell_ovp"] == '3.65'
    assert c.published[f"{DEV}/settings/charge_utp"] == '-5'
    assert c.published[f"{DEV}/settings/cell_count"] == '8'
    assert c.published[f"{DEV}/settings/display_always_on"] == 'ON'
    assert c.published[f"{DEV}/settings/heating"] == 'OFF'
    assert all(retain is False for _, _, retain in c.log)


def test_settings_states_exclude_and_force():
    c = _Recorder()
    pub = MqttSettingsPublisher(c, DEV, exclude={'charging_float_mode'})
    pub.states({'charging_float_mode': True, 'cell_ovp': 3.65})
    assert f"{DEV}/settings/charging_float_mode" not in c.published
    n = len(c.log)
    pub.states({'cell_ovp': 3.65})  # unchanged: deduplicated like other states
    assert len(c.log) == n
    pub.states({'cell_ovp': 3.65}, force=True)  # after discovery: HA must see it
    assert len(c.log) == n + 1


def _jk_with_fixture(ignore_writes=False):
    import os
    from bmslib.models.dummy import JKDummy
    from bmslib.models.jikong import JKBt
    with open(os.path.join(os.path.dirname(__file__), 'data', 'jk_settings_b2a8s30p_fw15.26.bin'), 'rb') as f:
        frame = f.read()
    bms = JKBt("00:11:22:33:44:55", name="jk1")
    bms.WRITE_SETTLE_S = bms.READBACK_DELAY_S = 0  # write timing is covered in test_jk_settings_driver
    client = JKDummy(is_new_11x=True, settings_frame=frame, ignore_writes=ignore_writes, stream=False)
    bms.client = client
    bms.char_handle_write = JKBt.CHAR_UUID
    bms.is_new_11fw_32s = bms.settings_protocol_confirmed = True
    asyncio.run(client.start_notify(JKBt.CHAR_UUID, bms._notification_handler))
    return bms


@pytest.mark.parametrize('payload, ignore_writes', [('9', False), ('6', False), ('3', True)])
def test_snap_back_reaches_the_broker(tmp_path, payload, ignore_writes):
    """After a rejected (out of range / cross-field) or failed write HA must get the real value
    again, even though it is the same value published seconds ago (mqtt_single_out deduplicates)."""
    bms = _jk_with_fixture(ignore_writes)
    c = _Recorder()
    pub = MqttSettingsPublisher(c, DEV)
    svc = JkSettingsService('jk1', frozenset({'charge_utp'}), workdir=str(tmp_path))
    asyncio.run(svc.tick(bms, pub))
    topic = f"{DEV}/settings/charge_utp"
    assert [p for t, p, _ in c.log if t == topic] == ['2']
    svc.enqueue('charge_utp', payload)
    asyncio.run(svc.tick(bms, pub))
    assert [p for t, p, _ in c.log if t == topic] == ['2', '2']


def test_last_change_always_publishes():
    c = _Recorder()
    pub = MqttSettingsPublisher(c, DEV)
    pub.last_change('Rejected cell_ovp 9: out of range')
    pub.last_change('Rejected cell_ovp 9: out of range')
    assert [p for t, p, _ in c.log if t == f"{DEV}/settings/last_change"] == [
        'Rejected cell_ovp 9: out of range'] * 2
    pub.last_change('x' * 400)
    assert len(c.published[f"{DEV}/settings/last_change"]) <= 255  # HA sensor state limit


# ---- sampler wiring

class _JkBms:
    """Just enough of a JKBt for BmsSampler + JkSettingsService."""
    name = 'jk_fake'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __init__(self, float_charger):
        self.float_charger = float_charger
        self.settings = None
        self.settings_raw = None
        self.t0 = time.time()
        self.k = 0

    def __str__(self):
        return 'FakeJk'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        self.k += 1
        return BmsSample(voltage=26.0, current=1.0, soc=60.0, timestamp=self.t0 + self.k)

    async def fetch_voltages(self):
        return [3300] * 8

    async def fetch_device_info(self):
        return None

    async def has_float_charger(self):
        return self.float_charger

    def supports_settings(self):
        return True

    async def refresh_settings(self):
        self.settings = {'cell_ovp': 3.65, 'charging_float_mode': True, 'display_always_on': False}
        return self.settings

    def debug_data(self):
        return None


def _jk_sampler(bms, client, writable=frozenset({'charging_float_mode', 'display_always_on'})):
    svc = JkSettingsService(bms.name, writable)
    s = BmsSampler(bms, mqtt_client=client, dt_max_seconds=120, expire_after_seconds=60, publish_period=3600,
                   settings_service=svc)
    return s


def test_sampler_wires_the_mqtt_settings_publisher():
    s = _jk_sampler(_JkBms(False), _Recorder())
    assert isinstance(s.settings_publisher, MqttSettingsPublisher)
    assert s.settings_publisher.device_topic == s.mqtt_topic_prefix
    # no MQTT, or no settings service: no publisher (service falls back to the no-op one)
    assert _jk_sampler(_JkBms(False), None).settings_publisher is None
    assert BmsSampler(_JkBms(False), mqtt_client=_Recorder(), dt_max_seconds=120,
                      expire_after_seconds=60).settings_publisher is None


@pytest.fixture
def _no_sleep(monkeypatch):
    import bmslib.sampling as sampling

    async def _sleep(*a, **kw):
        pass

    monkeypatch.setattr(sampling.asyncio, 'sleep', _sleep)


def _run_sampler(float_charger):
    c = _Recorder()
    s = _jk_sampler(_JkBms(float_charger), c)
    asyncio.run(s())
    return c


def test_sampler_publishes_settings_discovery_states_and_subscribes(_no_sleep):
    c = _run_sampler(False)
    dev = 'jk_fake'
    assert f"homeassistant/switch/{dev}/setting_charging_float_mode/config" in c.published
    assert f"homeassistant/switch/{dev}/setting_display_always_on/config" in c.published
    assert c.published[f"{dev}/settings/cell_ovp"] == '3.65'
    assert c.published[f"{dev}/settings/charging_float_mode"] == 'ON'
    subs = {t for t, _ in c.subscribed}
    assert f"homeassistant/switch/{dev}/display_always_on/set" in subs
    assert f"homeassistant/switch/{dev}/charging_float_mode/set" in subs
    # states are published again after the discovery so HA sees them right away
    assert [t for t, _, _ in c.log].index(f"homeassistant/sensor/{dev}/setting_cell_ovp/config") < \
        max(i for i, (t, _, _) in enumerate(c.log) if t == f"{dev}/settings/cell_ovp")


def test_no_duplicate_float_switch_on_pb(_no_sleep):
    """JK-PB already exposes `float_charge` as a sample switch; no second entity for the same bit."""
    c = _run_sampler(True)
    assert not [t for t in c.published if 'charging_float_mode' in t]
    assert not [t for t, _ in c.subscribed if 'charging_float_mode' in t]
    assert "homeassistant/switch/jk_fake/setting_display_always_on/config" in c.published


def test_float_switch_withheld_while_float_charger_unknown(_no_sleep):
    class _Unknown(_JkBms):
        async def has_float_charger(self):
            raise TimeoutError('device info')

    c = _Recorder()
    s = _jk_sampler(_Unknown(False), c)
    asyncio.run(s())
    assert not [t for t in c.published if 'charging_float_mode' in t]
    assert c.subscribed == []  # subscribed once the exclusion is known
    assert c.published['jk_fake/settings/cell_ovp'] == '3.65'


def test_slow_settings_tick_does_not_expire_the_fetched_sample(_no_sleep, monkeypatch):
    """Settings writes (~2 s each) run between fetch() and the expiry check: their duration must
    not turn the fresh sample into a SampleExpiredError."""
    import bmslib.sampling as sampling
    clock = [1_800_000_000.0]
    monkeypatch.setattr(sampling.time, 'time', lambda: clock[0])

    class _Fresh(_JkBms):
        async def fetch(self):
            return BmsSample(voltage=26.0, current=1.0, soc=60.0, timestamp=clock[0])

    class _SlowService:
        writable = frozenset()

        async def tick(self, bms, publish):
            clock[0] += 120  # e.g. several writes, longer than expire_after and MIN_VALUE_EXPIRY

    s = BmsSampler(_Fresh(False), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60,
                   publish_period=3600, settings_service=_SlowService())
    res = asyncio.run(s())
    assert res is not None and s.num_samples == 1


def _first(log, pred):
    return next(i for i, (t, _, _) in enumerate(log) if pred(t))


def _settings_config(t, node):
    return t.startswith('homeassistant/') and f'/{node}/setting' in t and t.endswith('/config')


def test_publisher_sends_discovery_before_the_first_states():
    c = _Recorder()
    pub = MqttSettingsPublisher(c, DEV, writable=frozenset({'charge_utp'}), expire_after_seconds=30)
    pub.states({'cell_ovp': 3.65, 'charge_utp': 2.0})
    first_state = _first(c.log, lambda t: t.startswith(f"{DEV}/settings/"))
    configs = [i for i, (t, _, _) in enumerate(c.log) if _settings_config(t, NODE)]
    assert configs and max(configs) < first_state
    assert c.config(f"homeassistant/number/{NODE}/setting_charge_utp/config")['command_topic']
    n = len(c.log)
    pub.states({'cell_ovp': 3.65, 'charge_utp': 2.0})  # discovery once, unchanged states deduplicated
    assert len(c.log) == n


def test_publisher_rediscovers_when_the_entities_change():
    c = _Recorder()
    pub = MqttSettingsPublisher(c, DEV, exclude={'charging_float_mode'}, writable=frozenset({'charging_float_mode'}),
                                expire_after_seconds=30)
    pub.states({'cell_ovp': 3.65, 'charging_float_mode': True})
    assert f"homeassistant/switch/{NODE}/setting_charging_float_mode/config" not in c.published
    n = len(c.log)
    pub.exclude = frozenset()  # e.g. float charger known now: one more entity
    pub.states({'cell_ovp': 3.65, 'charging_float_mode': True})
    new = [t for t, _, _ in c.log[n:]]
    assert new.index(f"homeassistant/switch/{NODE}/setting_charging_float_mode/config") < \
        new.index(f"{DEV}/settings/charging_float_mode")
    assert f"{DEV}/settings/cell_ovp" in new  # forced after the new discovery


def test_publisher_without_writable_sends_no_discovery():
    c = _Recorder()
    MqttSettingsPublisher(c, DEV).states({'cell_ovp': 3.65})
    assert [t for t, _, _ in c.log] == [f"{DEV}/settings/cell_ovp"]


def test_sampler_first_settings_states_after_discovery(_no_sleep):
    """After a restart the settings entities must not stay 'unavailable' until the next refresh:
    their HA configs go out before the first states."""
    c = _run_sampler(False)
    first_state = _first(c.log, lambda t: t.startswith('jk_fake/settings/') and not t.endswith('last_change'))
    configs = [i for i, (t, _, _) in enumerate(c.log) if _settings_config(t, 'jk_fake')]
    assert configs and min(configs) < first_state
    live = {c.log[i][0] for i in configs if c.log[i][1]}
    live_before = {c.log[i][0] for i in configs if c.log[i][1] and i < first_state}
    assert live_before == live  # every entity's config before the first state
    assert len(live) == len([s for s in SETTINGS.values() if s.read is not None]) + 1  # + settings_last_change


def test_states_sent_again_shortly_after_a_discovery(monkeypatch):
    """States right behind the configs can reach the broker before HA subscribed to the new
    entities' state topics (not retained: lost); they go out once more a moment later."""
    clock = [1000.0]
    monkeypatch.setattr(mqtt_util.time, 'time', lambda: clock[0])
    c = _Recorder()
    pub = MqttSettingsPublisher(c, DEV, writable=frozenset(), expire_after_seconds=30)
    settings = {'cell_ovp': 3.65}
    pub.states(settings)
    topic = f"{DEV}/settings/cell_ovp"
    assert [t for t, _, _ in c.log].count(topic) == 1
    pub.resend_after_discovery(settings)  # too early
    assert [t for t, _, _ in c.log].count(topic) == 1
    clock[0] += mqtt_util.SETTINGS_RESEND_AFTER_DISCOVERY_S
    pub.resend_after_discovery(settings)
    assert [t for t, _, _ in c.log].count(topic) == 2
    clock[0] += 60
    pub.resend_after_discovery(settings)  # once per discovery
    assert [t for t, _, _ in c.log].count(topic) == 2


def test_sampler_resends_settings_states_after_discovery(_no_sleep, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(mqtt_util.time, 'time', lambda: clock[0])
    c = _Recorder()
    s = _jk_sampler(_JkBms(False), c)
    asyncio.run(s())
    n = [t for t, _, _ in c.log].count('jk_fake/settings/cell_ovp')
    clock[0] += 5
    asyncio.run(s())
    assert [t for t, _, _ in c.log].count('jk_fake/settings/cell_ovp') == n + 1
