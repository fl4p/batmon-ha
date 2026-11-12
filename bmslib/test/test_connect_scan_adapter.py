"""A failed connect starts a discovery scan; it must scan on the configured adapter.

With `adapter: hci1` the scan ran on the default controller (hci0), where the BMS
was out of range, so the scan never helped and the error context misled.
"""

import asyncio
import types

import bleak
import pytest

import bmslib.bt
from bmslib.bt import BtBms

NotFound = getattr(bleak.exc, 'BleakDeviceNotFoundError', bleak.exc.BleakError)


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, fmt, *args):
        self.lines.append(fmt % args if args else str(fmt))

    info = warning = error = debug = _rec


class _Client:
    async def connect(self, timeout):
        raise NotFound('20:A1:11:02:23:45', 'could not connect')


def _run(adapter, proxy, monkeypatch):
    seen = {}

    async def fake_discovery(logger, timeout=5, adapter=None):
        seen['adapter'] = adapter
        return []

    monkeypatch.setattr(bmslib.bt, 'bt_discovery', fake_discovery)
    monkeypatch.setattr(bmslib.bt, 'scanner_is_proxy', lambda: proxy)
    monkeypatch.setattr(BtBms, 'shutdown', False, raising=False)
    bms = types.SimpleNamespace(client=_Client(), logger=_Log(), verbose_log=False,
                                name='ant24', address='20:A1:11:02:23:45', _adapter=adapter)
    with pytest.raises(NotFound):
        asyncio.run(BtBms._connect_client(bms, timeout=1))
    return seen['adapter']


def test_scan_uses_configured_adapter(monkeypatch):
    assert _run('hci1', False, monkeypatch) == 'hci1'


def test_scan_default_without_adapter(monkeypatch):
    assert _run(None, False, monkeypatch) is None


def test_proxy_scan_gets_no_local_adapter(monkeypatch):
    assert _run('hci1', True, monkeypatch) is None


def test_no_nested_scan_from_scanner_connect(monkeypatch):
    """_connect_with_scanner already scans on this adapter; a second scan there would clash."""
    called = []

    async def fake_discovery(logger, timeout=5, adapter=None):
        called.append(adapter)

    monkeypatch.setattr(bmslib.bt, 'bt_discovery', fake_discovery)
    monkeypatch.setattr(BtBms, 'shutdown', False, raising=False)
    bms = types.SimpleNamespace(client=_Client(), logger=_Log(), verbose_log=False,
                                name='ant24', address='20:A1:11:02:23:45', _adapter='hci1')
    with pytest.raises(NotFound):
        asyncio.run(BtBms._connect_client(bms, timeout=1, scan_on_not_found=False))
    assert called == []


def test_failed_scan_keeps_not_found(monkeypatch):
    async def broken_discovery(logger, timeout=5, adapter=None):
        raise RuntimeError('org.bluez.Error.InProgress')

    monkeypatch.setattr(bmslib.bt, 'bt_discovery', broken_discovery)
    monkeypatch.setattr(bmslib.bt, 'scanner_is_proxy', lambda: False)
    monkeypatch.setattr(BtBms, 'shutdown', False, raising=False)
    bms = types.SimpleNamespace(client=_Client(), logger=_Log(), verbose_log=False,
                                name='ant24', address='20:A1:11:02:23:45', _adapter='hci1')
    with pytest.raises(NotFound):
        asyncio.run(BtBms._connect_client(bms, timeout=1))
    assert any('InProgress' in l for l in bms.logger.lines)


class _Scanner:
    kwargs = None

    def __init__(self, **kwargs):
        type(self).kwargs = kwargs
        self.discovered_devices = []

    async def start(self):
        raise RuntimeError('stop here')

    async def stop(self):
        pass


@pytest.mark.parametrize('proxy,expect', [(False, {'adapter': 'hci1'}), (True, {})])
def test_scanner_connect_adapter(monkeypatch, proxy, expect):
    monkeypatch.setattr(bmslib.bt, 'BleakScanner', _Scanner)
    monkeypatch.setattr(bmslib.bt, 'scanner_is_proxy', lambda: proxy)
    monkeypatch.setattr(BtBms, 'shutdown', False, raising=False)
    bms = types.SimpleNamespace(client=_Client(), logger=_Log(), verbose_log=False, _pending_disconnect_call=False,
                                name='ant24', address='20:A1:11:02:23:45', _adapter='hci1')
    with pytest.raises(RuntimeError):
        asyncio.run(BtBms._connect_with_scanner(bms, timeout=1))
    assert _Scanner.kwargs == expect
