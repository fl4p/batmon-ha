"""Regression tests for #415: `pin:` must reach the aiobmsble-backed models.

construct_bms() passes the configured `pin:` to every model as `psk=`. The
aiobmsble wrapper used to swallow it in **kwargs, so for `felicity`, `daly_ble`
and every other `_ble` type a PIN was a silent no-op — and a BMS that only
serves GATT to a bonded central (Felicity firmware that renames the pack to
`SolarB_*`) could not be read at all, with nothing in the log saying why.

The bond itself is BlueZ's, not bleak's — see bmslib/pairing.py. What the
wrapper owes is: hand the address and the pin to that code before the aiobmsble
driver connects, once, and never let a pairing problem raise.
"""

import asyncio
import inspect

import pytest

import bmslib.pairing as pairing
from bmslib.models.BLE_BMS_wrap import BMS, BLEDeviceResolver


class _FakeBleDevice:
    def __init__(self, address):
        self.address = address
        self.name = address
        self.details = {"path": "/org/bluez/hci0/dev_" + address.replace(":", "_")}


class _FakeBaseBMS:
    def __init__(self, ble_device, config=None, keep_alive=False):
        self.ble_device = ble_device

        class _Client:
            is_connected = False

        self._client = _Client()

    async def _connect(self):
        _events.append("connect")
        self._client.is_connected = True

    async def disconnect(self, reset=False):
        self._client.is_connected = False


_events: list = []
_bond_calls: list = []
#: what the faked bond_with_pin returns
_bond_result = [pairing.PAIRED]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _events.clear()
    _bond_calls.clear()
    _bond_result[0] = pairing.PAIRED
    BLEDeviceResolver.devices = {}

    async def fake_resolve(addr, adapter=None):
        return _FakeBleDevice(addr)

    async def fake_bond(address, pin, adapter=None, name=None, timeout=None):
        _events.append("pair")
        _bond_calls.append(dict(address=address, pin=pin, adapter=adapter, name=name))
        return _bond_result[0]

    monkeypatch.setattr(BLEDeviceResolver, "resolve", staticmethod(fake_resolve))
    monkeypatch.setattr(pairing, "bond_with_pin", fake_bond)
    # default: a local adapter, i.e. bonding is possible
    monkeypatch.setattr("bmslib.bt.scanner_is_proxy", lambda: False)
    yield


def _make_bms(psk=None, adapter=None):
    return BMS("AA:BB:CC:DD:EE:FF", type="felicity", blebms_class=_FakeBaseBMS,
               keep_alive=True, name="pack", psk=psk, adapter=adapter)


def test_pin_is_accepted_not_swallowed():
    """`psk` must be a named parameter, not something **kwargs eats in silence."""
    assert "psk" in inspect.signature(BMS.__init__).parameters
    assert _make_bms(psk="123456")._psk == "123456"


def test_bonds_before_the_driver_connects():
    bms = _make_bms(psk="123456")
    asyncio.run(bms.connect())

    # order is the whole point: aiobmsble's _connect() is what the unbonded
    # device drops, so the bond must already be there when it runs
    assert _events == ["pair", "connect"]
    assert _bond_calls == [dict(address="AA:BB:CC:DD:EE:FF", pin="123456",
                                adapter=None, name="pack")]


def test_no_pin_does_not_pair():
    bms = _make_bms(psk=None)
    asyncio.run(bms.connect())
    assert _events == ["connect"]
    assert not _bond_calls


def test_bonds_only_once_per_process():
    bms = _make_bms(psk="123456")
    asyncio.run(bms.connect())
    for _ in range(3):
        bms.ble_bms._client.is_connected = False
        asyncio.run(bms.connect())
    # a bond is persistent in BlueZ; repeating it would add a D-Bus round trip
    # to every poll of a non-keep-alive device
    assert _events.count("pair") == 1
    assert _events.count("connect") == 4


def test_already_paired_counts_as_bonded():
    _bond_result[0] = pairing.ALREADY_PAIRED
    bms = _make_bms(psk="123456")
    asyncio.run(bms.connect())
    bms.ble_bms._client.is_connected = False
    asyncio.run(bms.connect())
    assert _events.count("pair") == 1


def test_failed_bond_is_retried_on_the_next_connect():
    """A failed bond must not latch — otherwise one bad attempt (BMS asleep,
    adapter busy) leaves the device unbonded until the add-on restarts."""
    _bond_result[0] = pairing.FAILED
    bms = _make_bms(psk="123456")
    asyncio.run(bms.connect())
    bms.ble_bms._client.is_connected = False
    _bond_result[0] = pairing.PAIRED
    asyncio.run(bms.connect())
    assert _events.count("pair") == 2
    assert bms._psk_paired is True


def test_unsupported_is_not_retried():
    """UNSUPPORTED means there is no BlueZ in this stack at all (bumble, bluek,
    no D-Bus bindings). That cannot change here — warn once, do not re-ask."""
    _bond_result[0] = pairing.UNSUPPORTED
    bms = _make_bms(psk="123456")
    asyncio.run(bms.connect())
    bms.ble_bms._client.is_connected = False
    asyncio.run(bms.connect())
    assert _events.count("pair") == 1
    assert _events.count("connect") == 2


def test_a_raising_bond_does_not_break_the_connect(monkeypatch):
    async def boom(*a, **kw):
        _events.append("pair")
        raise RuntimeError("D-Bus went away")

    monkeypatch.setattr(pairing, "bond_with_pin", boom)
    bms = _make_bms(psk="123456")
    asyncio.run(bms.connect())  # the connect below reports what the device does
    assert _events == ["pair", "connect"]
    assert bms.is_connected


def test_proxy_stack_warns_instead_of_pairing(monkeypatch):
    """An ESPHome node can be told to pair, but has no agent to answer a PIN
    request, so a `pin:` cannot be delivered over that stack. Say it once."""
    monkeypatch.setattr("bmslib.bt.scanner_is_proxy", lambda: True)
    bms = _make_bms(psk="123456")
    asyncio.run(bms.connect())
    asyncio.run(bms.connect())
    assert _events == ["connect", "connect"]
    assert not _bond_calls


def test_adapter_is_passed_through():
    bms = _make_bms(psk="123456", adapter="hci1")
    asyncio.run(bms.connect())
    assert _bond_calls[0]["adapter"] == "hci1"


def test_pairing_gives_up_after_a_few_failures():
    """_pair_psk runs under the process-wide ConnectLock, so a pack that never
    bonds must not make every other device wait on a bond attempt once per
    poll. It gives up, says so, and lets the connect proceed unbonded."""
    from bmslib.models.BLE_BMS_wrap import PSK_MAX_ATTEMPTS

    _bond_result[0] = pairing.FAILED
    bms = _make_bms(psk="123456")
    for _ in range(PSK_MAX_ATTEMPTS + 3):
        bms.ble_bms and setattr(bms.ble_bms._client, "is_connected", False)
        asyncio.run(bms.connect())
    assert _events.count("pair") == PSK_MAX_ATTEMPTS
    assert _events.count("connect") == PSK_MAX_ATTEMPTS + 3
