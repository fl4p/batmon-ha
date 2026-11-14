"""keep_alive link diagnostics: connect count, link age, RSSI/TX power on bluek."""
import asyncio
import json
import time
from types import SimpleNamespace

import paho.mqtt.client as paho

import bmslib.link_stats as link_stats
from bmslib.bms import BmsSample
from bmslib.link_stats import LinkStats
from bmslib.mqtt_util import publish_hass_discovery
from bmslib.test.test_incomplete_connect import _Bms as _IncompleteBms, _enter


class _Bms(_IncompleteBms):
    """Sets connect_time like BtBms._connect_client(), which the base fake skips."""

    async def connect(self, timeout=20):
        if not self.client.is_connected:
            self._connect_time = time.time()
        await super().connect(timeout)


class _BluekClient:
    """bluek's get_conn_info(): a ConnInfo with Optional fields, or an exception."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def get_conn_info(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _poll(link, client):
    asyncio.run(link.poll_conn_info(client))


def test_values_while_connected_and_after_the_link_drops():
    link = LinkStats('bms')
    link.on_connect()
    _poll(link, _BluekClient(SimpleNamespace(rssi=-71, tx_power=9, max_tx_power=20)))

    assert link.values(connected=True) == {'link/connects': 1, 'link/age': 0, 'link/rssi': -71, 'link/tx_power': 9}
    # down: only the count, so age and RSSI expire instead of showing the dead link's values
    assert link.values(connected=False) == {'link/connects': 1}


def test_no_rssi_on_a_stack_without_get_conn_info():
    link = LinkStats('bms')
    link.on_connect()
    _poll(link, object())  # bleak/BlueZ D-Bus, ESPHome proxy
    assert set(link.values(connected=True)) == {'link/connects', 'link/age'}


def test_an_unknown_value_is_left_out_not_published_as_a_number():
    link = LinkStats('bms')
    link.on_connect()
    _poll(link, _BluekClient(SimpleNamespace(rssi=None, tx_power=None, max_tx_power=None)))
    assert 'link/rssi' not in link.values(connected=True)
    assert 'link/tx_power' not in link.values(connected=True)


def test_a_failing_read_is_retried_later_and_warned_once_per_link(caplog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(link_stats.time, 'monotonic', lambda: now[0])
    link = LinkStats('bms')
    link.on_connect()
    client = _BluekClient(TimeoutError())  # a transient controller hiccup
    _poll(link, client)
    _poll(link, client)
    assert client.calls == 1  # not hammered
    assert 'link/rssi' not in link.values(connected=True)

    now[0] += link_stats.CONN_INFO_RETRY_S
    _poll(link, client)
    assert client.calls == 2
    assert caplog.text.count('reading link RSSI failed') == 1  # the retry logs at debug

    client.result = SimpleNamespace(rssi=-60, tx_power=3, max_tx_power=20)
    now[0] += link_stats.CONN_INFO_RETRY_S
    _poll(link, client)
    assert link.values(connected=True)['link/rssi'] == -60


def test_a_new_link_retries_a_failed_read_at_once(caplog):
    link = LinkStats('bms')
    link.on_connect()
    client = _BluekClient(PermissionError('mgmt status 0x14'))
    _poll(link, client)
    link.on_connect()
    _poll(link, client)
    assert client.calls == 2
    assert caplog.text.count('reading link RSSI failed') == 2  # once per link


def test_a_reply_without_the_fields_is_an_error_not_a_crash():
    link = LinkStats('bms')
    link.on_connect()
    _poll(link, _BluekClient(object()))
    assert 'link/rssi' not in link.values(connected=True)


def test_a_good_read_after_a_value_is_cleared_on_reconnect():
    link = LinkStats('bms')
    link.on_connect()
    _poll(link, _BluekClient(SimpleNamespace(rssi=-71, tx_power=9, max_tx_power=20)))
    link.on_connect()  # values of the old link must not carry over
    assert 'link/rssi' not in link.values(connected=True)


def test_a_hanging_read_times_out(monkeypatch):
    class _Hang:
        async def get_conn_info(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(link_stats, 'CONN_INFO_TIMEOUT', 0.01)
    link = LinkStats('bms')
    link.on_connect()
    _poll(link, _Hang())
    assert 'link/rssi' not in link.values(connected=True)


def _cycle(bms, link):
    """What BmsSampler._sample_inner does around `async with bms`."""
    was_connected = bms.is_connected
    before = bms.connect_time
    err = asyncio.run(_enter(bms))
    if err is None:
        link.observe(was_connected, before, bms.connect_time)
    return err


def test_connects_count_each_new_link_and_not_each_sample():
    bms, link = _Bms(), LinkStats('bms')
    for _ in range(3):
        assert _cycle(bms, link) is None
    assert link.connects == 1

    bms.client.is_connected = False  # the link dropped between two samples
    bms._on_disconnect(bms.client)
    assert _cycle(bms, link) is None
    assert link.connects == 2


def test_a_link_rebuilt_after_a_half_finished_connect_is_counted():
    bms, link = _Bms(fail_connect=True), LinkStats('bms')
    assert isinstance(_cycle(bms, link), TimeoutError)
    bms.fail_connect = False
    # was_connected is True here: the link of the failed connect is still up (#391)
    assert bms.is_connected
    assert _cycle(bms, link) is None
    assert bms.client.num_connects == 2
    assert link.connects == 1  # the failed cycle never sampled, the rebuilt link did


def test_aiobmsble_connect_time_is_fixed_so_only_was_connected_counts():
    link = LinkStats('bms')
    assert link.observe(False, 100.0, 100.0)
    assert not link.observe(True, 100.0, 100.0)
    assert link.connects == 1


def test_a_new_client_object_is_a_new_link():
    """aiobmsble reconnects inside async_update() with a new client; was_connected
    is True at the next cycle and connect_time does not move."""
    link, c1, c2 = LinkStats('bms'), object(), object()
    assert link.observe(False, 100.0, 100.0, c1)
    assert not link.observe(True, 100.0, 100.0, c1)
    assert link.observe(True, 100.0, 100.0, c2)
    assert not link.observe(True, 100.0, 100.0, c2)
    assert link.connects == 2


def _discovery(link):
    published = {}

    class Result:
        rc = paho.MQTT_ERR_SUCCESS

    class Client:
        def publish(self, topic, payload, retain=False):
            published[topic] = json.loads(payload)
            return Result()

    publish_hass_discovery(Client(), "test/link", 20, BmsSample(voltage=13.2, current=0), 4, [], link=link)
    return {t.split('/')[-2]: m for t, m in published.items() if '/_link_' in t}


def test_discovery_only_for_values_that_exist():
    assert _discovery(None) == {}
    d = _discovery({'link/connects': 3, 'link/age': 12})
    assert set(d) == {'_link_connects', '_link_age'}
    assert d['_link_age']['unit_of_measurement'] == 's'
    assert 'unit_of_measurement' not in d['_link_connects']
    assert all(m['entity_category'] == 'diagnostic' for m in d.values())
    # 30 s publish period, so not the sample expiry (20 s here) or they would flicker
    assert all(m['expire_after'] == link_stats_expire() for m in d.values())


def test_discovery_of_rssi_and_tx_power():
    d = _discovery({'link/connects': 1, 'link/age': 5, 'link/rssi': -70, 'link/tx_power': 9})
    assert d['_link_rssi']['device_class'] == 'signal_strength'
    assert d['_link_rssi']['unit_of_measurement'] == 'dBm'
    assert d['_link_tx_power']['unit_of_measurement'] == 'dBm'


def link_stats_expire():
    from bmslib.mqtt_util import LINK_EXPIRE_S
    return LINK_EXPIRE_S


def _sampler(bms, **kw):
    from bmslib.sampling import BmsSampler
    kw.setdefault('expire_after_seconds', 60)
    return BmsSampler(bms, dt_max_seconds=120, **kw)


def test_sampler_reads_link_values_for_keep_alive_only():
    bms = _Bms()
    asyncio.run(_enter(bms))
    s = _sampler(bms, mqtt_client=None)
    s.link.on_connect()
    asyncio.run(s.update_link())
    assert set(s._link_values) == {'link/connects', 'link/age'}

    bms.set_keep_alive(False)  # every sample is its own short connection
    asyncio.run(s.update_link())
    assert s._link_values is None


def test_sampler_reads_rssi_from_the_bms_client():
    bms = _Bms()
    asyncio.run(_enter(bms))
    bms.client.get_conn_info = _BluekClient(SimpleNamespace(rssi=-66, tx_power=9, max_tx_power=20)).get_conn_info
    s = _sampler(bms, mqtt_client=None)
    s.link.on_connect()
    asyncio.run(s.update_link())
    assert s._link_values['link/rssi'] == -66


class _Mqtt:
    """Records (topic, payload) in publish order."""

    def __init__(self):
        self.log = []

    def publish(self, topic, payload, retain=False):
        self.log.append((topic, payload))
        return SimpleNamespace(rc=paho.MQTT_ERR_SUCCESS)

    def subscribe(self, *a, **kw):
        return paho.MQTT_ERR_SUCCESS, 0

    def message_callback_add(self, *a, **kw):
        pass


def test_the_real_sampling_cycle_counts_links_and_publishes_after_discovery(monkeypatch):
    import bmslib.mqtt_util as mqtt_util
    from bmslib.models.dummy import DummyBt
    monkeypatch.setattr(mqtt_util, '_last_values', {})  # no dedupe across tests
    bms = DummyBt('dummy-link', name='dl', keep_alive=True)
    mqtt = _Mqtt()
    s = _sampler(bms, mqtt_client=mqtt)

    async def cycles(n):
        for _ in range(n):
            await s._sample_inner()

    asyncio.run(cycles(3))
    assert s.link.connects == 1

    topics = [t for t, _ in mqtt.log]
    disc = topics.index('homeassistant/sensor/dl/_link_connects/config')
    state = topics.index('dl/link/connects')
    assert state > disc, 'the state must follow the discovery so HA does not show unknown'
    assert dict(mqtt.log)['dl/link/connects'] == 1

    bms._connected = False  # the link dropped between two samples
    s.period_30s.state = True  # next cycle publishes the link values
    asyncio.run(cycles(1))
    assert s.link.connects == 2
    assert [p for t, p in mqtt.log if t == 'dl/link/connects'][-1] == 2


def test_expiry_follows_expire_values_after_0():
    published = {}

    class Client:
        def publish(self, topic, payload, retain=False):
            published[topic] = json.loads(payload)
            return SimpleNamespace(rc=paho.MQTT_ERR_SUCCESS)

    sample = BmsSample(voltage=13.2, current=0)
    for exp, want in ((0, 0), (20, 120), (600, 600)):
        published.clear()
        publish_hass_discovery(Client(), "t/x", exp, sample, 4, [], link={'link/age': 3})
        assert published['homeassistant/sensor/t_x/_link_age/config'].get('expire_after', 0) == want, exp
