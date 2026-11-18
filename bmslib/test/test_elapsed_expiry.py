"""Clock corrections must not freeze cached readings or MQTT refreshes."""
import asyncio
from types import SimpleNamespace

import pytest

from bmslib.cache import mem
from bmslib import mqtt_util


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(wall=10000., elapsed=100.)
    clock = SimpleNamespace(time=lambda: state.wall, monotonic=lambda: state.elapsed)
    monkeypatch.setattr(mem, 'time', clock)
    monkeypatch.setattr(mqtt_util, 'time', clock)
    monkeypatch.setattr(mqtt_util, '_last_values', {})
    monkeypatch.setattr(mqtt_util, '_last_publish_time', 0.)
    return state


@pytest.mark.parametrize('wall_step', [-3600, 3600])
def test_cache_lifetime_uses_elapsed_time_despite_clock_step(clock, wall_step):
    calls = []
    @mem.mem_cache_deco(ttl=30)
    async def read():
        calls.append(len(calls) + 1)
        return calls[-1]
    async def run():
        assert await read() == 1
        clock.elapsed += 5
        clock.wall += wall_step
        assert await read() == 1
        clock.elapsed += 26
        assert await read() == 2
    asyncio.run(run())


def test_cache_invalidation_instance_keys_and_unknown_results(clock):
    class Device:
        def __init__(self):
            self.value = None
            self.calls = 0
        @mem.mem_cache_deco(ttl=30)
        async def read(self):
            self.calls += 1
            if isinstance(self.value, Exception):
                raise self.value
            return self.value
    async def run():
        a, b = Device(), Device()
        assert await a.read() is None
        a.value = ValueError('no reply')
        with pytest.raises(ValueError):
            await a.read()
        a.value, b.value = 12, 24
        assert await a.read() == 12
        assert await b.read() == 24
        a.value = 13
        assert await a.read() == 12
        Device.read.invalidate(a)
        assert await a.read() == 13
        assert await b.read() == 24
        assert a.calls == 4
        assert b.calls == 1
    asyncio.run(run())


def test_daly_status_refreshes_after_clock_moves_backwards(clock):
    import struct
    from bmslib.models.daly import DalyBt
    bms = DalyBt('00:11:22:33:44:55', name='test-daly')
    replies = [struct.pack('>b??Bl', 1, True, True, 0, 10000),
               struct.pack('>b??Bl', 2, True, False, 0, 9000)]
    async def query(command):
        assert command == 0x93
        return replies.pop(0)
    bms._q = query
    async def run():
        assert (await bms._fetch_status())['discharging_mosfet'] is True
        clock.wall -= 3600
        clock.elapsed += 31
        status = await bms._fetch_status()
        assert status['discharging_mosfet'] is False
        assert status['capacity_ah'] == 9
        assert replies == []
    try:
        asyncio.run(run())
    finally:
        DalyBt._fetch_status.invalidate(bms)


@pytest.mark.parametrize('wall_step', [-3600, 3600])
def test_unchanged_mqtt_readings_refresh_on_elapsed_time(clock, wall_step):
    sent = []
    def publish(topic, data, **kwargs):
        sent.append((topic, data))
        return SimpleNamespace(rc=mqtt_util.paho.MQTT_ERR_SUCCESS)
    client = SimpleNamespace(publish=publish)
    mqtt_util.mqtt_single_out(client, 'battery/voltage', 12.5)
    assert mqtt_util.mqtt_last_publish_time() == clock.wall
    clock.elapsed += 5
    clock.wall += wall_step
    assert mqtt_util.mqtt_single_out(client, 'battery/voltage', 12.5) is False
    assert len(sent) == 1
    clock.elapsed += 6
    mqtt_util.mqtt_single_out(client, 'battery/voltage', 12.5)
    assert sent == [('battery/voltage', 12.5)] * 2
    # This API remains wall time for callers comparing it with time.time().
    assert mqtt_util.mqtt_last_publish_time() == clock.wall


def test_mqtt_changed_values_and_failed_sends_are_not_suppressed(clock):
    sent = []
    reply = SimpleNamespace(rc=mqtt_util.paho.MQTT_ERR_NO_CONN)
    def publish(topic, data, **kwargs):
        sent.append((topic, data))
        return reply
    client = SimpleNamespace(publish=publish)
    assert mqtt_util.mqtt_single_out(client, 'battery/current', 2) is False
    reply.rc = mqtt_util.paho.MQTT_ERR_SUCCESS
    mqtt_util.mqtt_single_out(client, 'battery/current', 2)
    mqtt_util.mqtt_single_out(client, 'battery/current', 3)
    assert sent == [('battery/current', 2), ('battery/current', 2), ('battery/current', 3)]
    assert mqtt_util.mqtt_single_out(client, 'battery/current', 3) is False
