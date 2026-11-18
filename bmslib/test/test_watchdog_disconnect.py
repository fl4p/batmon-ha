"""The "no data has been flowing" watchdog drops a link that stopped delivering.

Its two timestamps must come from the elapsed clock: the watchdog asks whether
data is still arriving, and a wall-clock correction must not answer that
differently -- a backwards step used to postpone the disconnect by the step.
"""
import asyncio
from types import SimpleNamespace

import pytest

import bmslib.ble.bt
from bmslib import sampling
from bmslib.bms import MIN_VALUE_EXPIRY, BmsSample
from bmslib.sampling import BmsSampler


class _Bms:
    name = 'fake'
    address = 'AA:BB:CC:DD:EE:FF'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __init__(self):
        self.fail = False
        self.disconnects = 0

    def __str__(self):
        return 'Bms(fake)'

    def debug_data(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        if self.fail:
            raise TimeoutError("notify timeout")
        return BmsSample(13.0, 1.0)

    async def fetch_voltages(self):
        return [3300]

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


@pytest.fixture(autouse=True)
def _no_ble_scan(monkeypatch):
    """The error handler runs a real 3 s BLE scan for a non-serial address."""
    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(bmslib.ble.bt, 'bt_diagnostics', _noop)


def test_no_data_flowing_disconnect_follows_elapsed_time(monkeypatch):
    bms = _Bms()
    sampler = BmsSampler(bms, mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60)
    wall, elapsed = [10000.0], [100.0]
    monkeypatch.setattr(sampling, 'time',
                        SimpleNamespace(time=lambda: wall[0], monotonic=lambda: elapsed[0]))
    sampler._last_diag_t = wall[0]

    asyncio.run(sampler())                 # a good sample stamps the watchdog
    assert sampler._t_wd_reset == elapsed[0]

    bms.fail = True
    elapsed[0] += 2 * max(MIN_VALUE_EXPIRY, sampler.expire_after_seconds) + 1
    wall[0] -= 3600                        # backwards correction

    with pytest.raises(TimeoutError):
        asyncio.run(sampler())
    assert bms.disconnects == 1
