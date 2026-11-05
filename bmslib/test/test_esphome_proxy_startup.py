"""#430: one unreachable ESPHome proxy held start-up forever.

bleak_esphome's APIConnectionManager.start() returns only after the first
successful connect; its ReconnectLogic retries an offline host indefinitely.
start_proxies() awaited each proxy in turn, so with one proxy offline
(the reporter's car was away) discovery and polling never began, even though
the other proxy was up. Start-up must go on after a bounded wait and leave the
offline proxy reconnecting in the background.
"""

import asyncio
import sys
import types

import pytest

from bmslib.esphome_proxy import bootstrap


class _Aborted(Exception):
    pass


class _FakeConn:
    """APIConnectionManager stand-in: start() blocks until the host comes up."""
    instances = []

    def __init__(self, config):
        self.host = config['address']
        self.up = asyncio.Event()
        self.stopped = False
        if self.host != 'offline':
            self.up.set()
        type(self).instances.append(self)

    async def start(self):
        await self.up.wait()
        if self.stopped:
            raise _Aborted()

    async def stop(self):
        self.stopped = True
        self.up.set()


class _FakeManager:
    async def async_setup(self):
        pass

    def async_stop(self):
        pass


@pytest.fixture
def fake_esphome(monkeypatch):
    mod = types.ModuleType('bleak_esphome')
    mod.APIConnectionManager = _FakeConn
    mod.ESPHomeStartAborted = _Aborted
    monkeypatch.setitem(sys.modules, 'bleak_esphome', mod)
    monkeypatch.setattr(bootstrap, '_manager', _FakeManager())
    monkeypatch.setattr(bootstrap, '_conns', [])
    monkeypatch.setattr(bootstrap, '_start_tasks', set(), raising=False)
    monkeypatch.setattr(bootstrap, 'STARTUP_WAIT_S', 0.2, raising=False)
    _FakeConn.instances = []
    return _FakeConn


def test_offline_proxy_does_not_block_startup(fake_esphome, caplog):
    async def run():
        proxies = [{'host': 'online', 'name': 'van'}, {'host': 'offline', 'name': 'car'}]
        # without the fix this never returns; the outer timeout turns that into a failure
        await asyncio.wait_for(bootstrap.start_proxies(proxies), 2)
        assert len(bootstrap._conns) == 2
        assert len(bootstrap._start_tasks) == 1  # the offline one is still connecting

        # the car comes back later: its start completes in the background
        fake_esphome.instances[1].up.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not bootstrap._start_tasks

        await bootstrap.stop_proxies()

    caplog.set_level('INFO')
    asyncio.run(run())
    assert 'car not reachable after 0 s' in caplog.text
    assert 'car up at offline' in caplog.text
    assert 'van up at online' in caplog.text


def test_all_offline_still_returns_and_stop_ends_pending(fake_esphome, caplog):
    async def run():
        await asyncio.wait_for(bootstrap.start_proxies([{'host': 'offline'}]), 2)
        assert len(bootstrap._start_tasks) == 1
        await bootstrap.stop_proxies()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not bootstrap._start_tasks
        assert fake_esphome.instances[0].stopped

    asyncio.run(run())
    assert 'no proxies connected yet' in caplog.text
