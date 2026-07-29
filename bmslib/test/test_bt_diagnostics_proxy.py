"""#391: with ble_stack=esphome, bt_diagnostics reported the *host's* hci
adapters next to a scan that never went near them.

The reporter's BMS were connected through an ESPHome proxy and are not in range
of the HA host at all, so the error context read

    bt_diagnostics C8:47:80:71:23:97: NOT seen during 3.0s scan on adapter=default
        (4 other devices in range, adapters=[{'index': 0, 'name': 'hci0', ...}])

which points at a local controller that is not in the BLE path. Report the proxy
scanners instead, and never pass a local `adapter:` to the proxy scanner.
"""

import asyncio

import bmslib.bt
import bmslib.esphome_proxy
from bmslib.bt import bt_diagnostics


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, fmt, *args):
        self.lines.append(fmt % args)

    info = warning = error = _rec

    def text(self):
        return '\n'.join(self.lines)


class _FakeScanner:
    """Stands in for a bleak scanner; records the kwargs it was constructed with."""
    last_kwargs = None
    discovered_devices = []

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    async def start(self):
        pass

    async def stop(self):
        pass


def _install(monkeypatch, module, sources=None, raises=False):
    """Point bmslib.bt at a fake scanner whose __module__ selects the stack."""
    scanner = type('S', (_FakeScanner,), {})
    scanner.__module__ = module
    monkeypatch.setattr(bmslib.bt, 'BleakScanner', scanner)
    monkeypatch.setattr(bmslib.bt, 'bt_adapters_info',
                        lambda: [dict(index=0, name='hci0', mac='43:34:B0:00:1F:AC', bus='UART')])

    def _sources():
        if raises:
            raise RuntimeError('manager not up')
        return sources
    monkeypatch.setattr(bmslib.esphome_proxy, 'proxy_sources', _sources)
    return scanner


def _run(log, adapter=None):
    return asyncio.run(bt_diagnostics('C8:47:80:09:C5:01', adapter, log, timeout=0))


def test_proxy_stack_reports_proxies_not_local_adapters(monkeypatch):
    _install(monkeypatch, 'habluetooth.wrappers', sources=['bluetooth-proxy-garaj'])
    log = _Log()
    result = _run(log)

    assert 'esphome-proxy' in log.text()
    assert 'bluetooth-proxy-garaj' in log.text()
    # the host's controller is not in the BLE path; naming it sends people chasing ghosts
    assert 'hci0' not in log.text()
    assert result['adapter'] == 'esphome-proxy'
    assert result['adapters'] == ['bluetooth-proxy-garaj']


def test_proxy_stack_never_gets_a_local_adapter_kwarg(monkeypatch):
    scanner = _install(monkeypatch, 'habluetooth.wrappers', sources=[])
    _run(_Log(), adapter='hci0')  # a leftover `adapter:` in the device config
    assert scanner.last_kwargs == {}


def test_unknown_proxies_do_not_read_as_none_connected(monkeypatch):
    # proxy_sources() blowing up must not be reported as "no proxies are connected"
    _install(monkeypatch, 'habluetooth.wrappers', raises=True)
    log = _Log()
    _run(log)
    assert 'proxies=?' in log.text()

    log = _Log()
    _install(monkeypatch, 'habluetooth.wrappers', sources=[])
    _run(log)
    assert 'proxies=[]' in log.text()


def test_local_stack_still_reports_adapters(monkeypatch):
    scanner = _install(monkeypatch, 'bleak.backends.bluezdbus.scanner')
    log = _Log()
    result = _run(log, adapter='hci0')

    assert 'adapter=hci0' in log.text()
    assert 'hci0' in log.text() and 'esphome-proxy' not in log.text()
    assert scanner.last_kwargs == dict(adapter='hci0')
    assert result['adapters'][0]['name'] == 'hci0'
