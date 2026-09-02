"""reconnect_interval_minutes: with keep_alive a BMS stays on the backend/proxy it
first connected through. The sampler optionally drops a healthy link after a
jittered interval so connect() picks again. Must go through force_disconnect(),
not disconnect(reset=...): every native model overrides disconnect(self) without
that kwarg (the PR #406 bug), and BLE_BMS_wrap is not a BtBms."""

import asyncio
import math

import bmslib.bt
from bmslib.sampling import BmsSampler


class _Bms:
    name = "fake"
    address = 'serial'
    is_virtual = False
    verbose_log = False
    connect_time = 0

    def __init__(self):
        self.is_connected = False
        self.forced = 0
        self.connects = 0

    def __str__(self):
        return "Bms(fake)"

    async def __aenter__(self):
        if not self.is_connected:
            self.connects += 1
            self.is_connected = True
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        raise TimeoutError("stop after connect")

    async def disconnect(self):  # native-model signature: no reset kwarg
        self.is_connected = False

    async def force_disconnect(self):
        assert bmslib.bt.ConnectLock.locked(), "teardown must run under ConnectLock"
        self.forced += 1
        self.is_connected = False

    def debug_data(self):
        return None


def _sampler(bms, interval):
    s = BmsSampler(bms, mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60,
                   reconnect_interval_s=interval)
    s.num_samples = 1
    return s


def _cycle(sampler):
    try:
        asyncio.run(sampler())
    except TimeoutError:
        pass


def test_reconnects_after_jittered_interval_and_rearms():
    bms = _Bms()
    s = _sampler(bms, 600)
    _cycle(s)
    assert bms.connects == 1
    assert 480 <= s._reconnect_due_s <= 720  # +/-20 %

    s._t_connected -= 479  # not due yet
    _cycle(s)
    assert bms.forced == 0 and bms.connects == 1

    s._t_connected -= 300  # past the widest jitter
    _cycle(s)
    assert bms.forced == 1
    assert bms.connects == 2  # same cycle reconnects
    assert 480 <= s._reconnect_due_s <= 720  # re-rolled on the new connect


def test_disabled_never_disconnects():
    bms = _Bms()
    s = _sampler(bms, None)
    _cycle(s)
    assert s._reconnect_due_s == math.inf
    s._t_connected = 0
    _cycle(s)
    assert bms.forced == 0 and bms.connects == 1


def test_negative_interval_is_off():
    bms = _Bms()
    s = _sampler(bms, -600)
    _cycle(s)
    s._t_connected = 0
    _cycle(s)
    assert bms.forced == 0 and bms.connects == 1


def test_only_when_connected():
    bms = _Bms()
    s = _sampler(bms, 600)
    s._t_connected = 0
    s._reconnect_due_s = 1
    _cycle(s)  # first connect; not connected before, so no teardown
    assert bms.forced == 0 and bms.connects == 1


def test_models_expose_force_disconnect():
    from bmslib.models.BLE_BMS_wrap import BMS as Wrap
    assert callable(getattr(bmslib.bt.BtBms, 'force_disconnect'))
    assert callable(getattr(Wrap, 'force_disconnect'))
