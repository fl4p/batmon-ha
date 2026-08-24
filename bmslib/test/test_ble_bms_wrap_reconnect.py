"""Regression test for #384: the aiobmsble wrapper must release the previous
BaseBMS instance before opening a new connection, otherwise a lingering acquired
notify FD makes every reconnect fail with `org.bluez.Error.NotPermitted: Notify
acquired` until the add-on is restarted.

The wrapper builds a *new* aiobmsble BaseBMS (and thus a new BleakClient) on each
connect(). If the previous one is orphaned without disconnecting, BlueZ still
holds its notify acquired and the fresh start_notify is rejected. The wrapper
must call disconnect(reset=True) on the old instance first.
"""

import asyncio

import pytest

from bmslib.models.BLE_BMS_wrap import BMS, BLEDeviceResolver


class _FakeBleDevice:
    def __init__(self, address):
        self.address = address
        self.name = address


class _FakeBaseBMS:
    """Stands in for an aiobmsble BaseBMS.

    Models the BlueZ 'Notify acquired' hazard with a shared flag: a connected
    instance 'acquires' the notify; _connect() raises if the notify is still
    acquired by a previous, un-disconnected instance. Only disconnect() releases
    it.
    """

    #: shared across instances, like the single characteristic in BlueZ
    notify_acquired = {"by": None}
    instances = []

    def __init__(self, ble_device, config=None, keep_alive=False):
        # aiobmsble < 0.26 took `keep_alive=`, >= 0.26 takes `config=BMSConfig(...)`.
        # Accept both so this fake does not pin the test to one of them; the
        # wrapper picks the right one (see test_bms_config_kwargs_match_installed).
        self.ble_device = ble_device
        self._keep_alive = config.keep_alive if config is not None else keep_alive
        self._connected = False
        self.disconnect_calls = []

        class _Client:
            is_connected = False

        self._client = _Client()
        _FakeBaseBMS.instances.append(self)

    async def _connect(self):
        acq = _FakeBaseBMS.notify_acquired
        if acq["by"] is not None and acq["by"] is not self:
            # BlueZ refuses start_notify while a previous client holds it
            raise Exception("[org.bluez.Error.NotPermitted] Notify acquired")
        acq["by"] = self
        self._connected = True
        self._client.is_connected = True

    async def disconnect(self, reset=False):
        self.disconnect_calls.append(reset)
        if _FakeBaseBMS.notify_acquired["by"] is self:
            _FakeBaseBMS.notify_acquired["by"] = None
        self._connected = False
        self._client.is_connected = False


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeBaseBMS.notify_acquired = {"by": None}
    _FakeBaseBMS.instances = []
    BLEDeviceResolver.devices = {}
    yield


def _make_bms(monkeypatch, keep_alive=True):
    async def fake_resolve(addr, adapter=None):
        return _FakeBleDevice(addr)

    monkeypatch.setattr(BLEDeviceResolver, "resolve", staticmethod(fake_resolve))
    return BMS("AA:BB:CC:DD:EE:FF", type="fake",
               blebms_class=_FakeBaseBMS, keep_alive=keep_alive, name="fake")


def test_reconnect_releases_stale_notify(monkeypatch):
    bms = _make_bms(monkeypatch)

    # first connect acquires the notify
    asyncio.run(bms.connect())
    first = bms.ble_bms
    assert _FakeBaseBMS.notify_acquired["by"] is first

    # simulate a dropped keep-alive link: is_connected goes False but the
    # instance (and its acquired notify) is still around
    first._client.is_connected = False

    # a reconnect must not raise 'Notify acquired' — the wrapper releases the old
    # instance first
    asyncio.run(bms.connect())
    second = bms.ble_bms

    assert second is not first
    assert _FakeBaseBMS.notify_acquired["by"] is second
    # the old instance was disconnected with reset=True before being orphaned
    assert first.disconnect_calls and first.disconnect_calls[-1] is True


def test_cleanup_failure_does_not_block_new_connect(monkeypatch):
    # If disconnecting the stale instance itself errors, connect() must not
    # propagate it — it still builds and connects a fresh instance (the new
    # _connect surfaces any notify that is genuinely still stuck).
    bms = _make_bms(monkeypatch)
    asyncio.run(bms.connect())
    first = bms.ble_bms
    first._client.is_connected = False

    async def boom(reset=False):
        raise Exception("close_stale_connections failed")

    first.disconnect = boom
    # first still 'holds' the notify (boom never releases it), so this also
    # checks connect() doesn't wedge on the leftover acquire in this fake; the
    # real recovery is the reset disconnect, exercised in the tests above.
    _FakeBaseBMS.notify_acquired["by"] = None  # emulate BlueZ releasing on its own
    asyncio.run(bms.connect())
    assert bms.ble_bms is not first
    assert bms.ble_bms._connected


def test_repeated_reconnects_never_wedge(monkeypatch):
    bms = _make_bms(monkeypatch)
    asyncio.run(bms.connect())
    for _ in range(5):
        bms.ble_bms._client.is_connected = False
        asyncio.run(bms.connect())  # would raise on the 2nd iter without the fix
    assert _FakeBaseBMS.notify_acquired["by"] is bms.ble_bms


def test_bms_config_kwargs_match_installed():
    """The wrapper must construct a BaseBMS the *installed* aiobmsble accepts.

    aiobmsble 0.26 replaced the `keep_alive` / `secret` constructor arguments
    with a single frozen `config: BMSConfig`. Passing the wrong one raises
    TypeError on every connect, for every `_ble` device — and nothing else in
    this suite instantiates a real BaseBMS subclass.
    """
    import inspect

    from aiobmsble.basebms import BaseBMS
    from bmslib.models.BLE_BMS_wrap import _bms_config_kwargs

    params = inspect.signature(BaseBMS.__init__).parameters
    for kw in _bms_config_kwargs(keep_alive=True):
        assert kw in params, f"BaseBMS.__init__ has no {kw!r} parameter: {list(params)}"
