"""Cancelling a slow port open must not retain an unowned bus reference."""
import asyncio
import threading

import pytest

import bmslib.uart as wired
from bmslib.test.test_shared_rs485_bus import _FakeTransport


@pytest.fixture(autouse=True)
def ports(monkeypatch):
    wired._reset_shared_ports()
    monkeypatch.setattr(wired, 'SerialTransport', _FakeTransport)
    yield
    wired._reset_shared_ports()


@pytest.mark.parametrize('fail_open', [False, True])
def test_cancelled_open_releases_reference_after_executor_finishes(monkeypatch, fail_open):
    async def run():
        client = wired.SerialBleakClientWrapper('/dev/test-cancel', baudrate=9600)
        entered, finish = threading.Event(), threading.Event()
        original_open = client.t.open

        def slow_open():
            entered.set()
            assert finish.wait(2)
            if fail_open:
                raise OSError('gateway unavailable')
            original_open()

        monkeypatch.setattr(client.t, 'open', slow_open)
        task = asyncio.create_task(client.connect())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await client.disconnect()
        finally:
            finish.set()
        for _ in range(100):
            if client.port.users == 0:
                break
            await asyncio.sleep(.01)
        assert client.port.users == 0
        assert not client.t.is_open
        monkeypatch.setattr(client.t, 'open', original_open)
        await client.connect()
        assert client.port.users == 1
        assert client.is_connected
        await client.disconnect()
        assert client.port.users == 0
        assert not client.t.is_open
    asyncio.run(run())


def test_cancelled_additional_handle_preserves_connected_sibling(monkeypatch):
    async def run():
        a = wired.SerialBleakClientWrapper('/dev/test-shared', baudrate=9600)
        b = wired.SerialBleakClientWrapper('/dev/test-shared', baudrate=9600)
        await a.connect()
        entered, finish = threading.Event(), threading.Event()
        original = a.port.ensure_open

        def delayed_ensure_open():
            entered.set()
            assert finish.wait(2)
            original()

        monkeypatch.setattr(a.port, 'ensure_open', delayed_ensure_open)
        task = asyncio.create_task(b.connect())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            finish.set()
        for _ in range(100):
            if a.port.users == 1:
                break
            await asyncio.sleep(.01)
        assert a.port.users == 1
        assert a.is_connected
        assert not b.is_connected
        await a.disconnect()
        assert a.port.users == 0
        assert not a.t.is_open
    asyncio.run(run())
