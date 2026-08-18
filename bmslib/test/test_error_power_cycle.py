"""#392: bt_power_cycle_on_error, the last-resort recovery for a wedged host stack.

The original proposal ran this from main.py's serial fetch loop on *any* exception
from *any* BMS, so a single hiccup on one battery cycled the radio for all of them.
It also drove `hciconfig hci0` directly, bypassing bt_power()'s ble_stack guards.

Here it lives next to the existing per-BMS backoff: it counts one BMS's forced
reconnects, resets on a good sample, and goes through bt_power().
"""

import asyncio
import time

import pytest

import bmslib.bt
from bmslib import sampling
from bmslib.sampling import BmsSampler


class _FakeBms:
    name = "fake"
    address = "AA:BB:CC:DD:EE:FF"
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __str__(self):
        return "FakeBms(fake)"

    def debug_data(self):
        return None

    async def disconnect(self):
        self.is_connected = False


@pytest.fixture
def bt_power_calls(monkeypatch):
    """Record bt_power() calls, and make the cycle instant and un-rate-limited."""
    calls = []
    monkeypatch.setattr(bmslib.bt, "bt_power", lambda on: calls.append(on))
    monkeypatch.setattr(sampling, "BT_POWER_CYCLE_SETTLE", 0)
    monkeypatch.setattr(sampling, "_t_last_bt_power_cycle", 0.0)
    # the BLE diagnostics scan is irrelevant here and would try to touch a real adapter
    monkeypatch.setattr(bmslib.bt, "bt_diagnostics", None)
    return calls


def _make_sampler(bms=None, **kw):
    sampler = BmsSampler(bms or _FakeBms(), mqtt_client=None, dt_max_seconds=120,
                         expire_after_seconds=60, **kw)
    sampler._last_diag_t = time.time()  # suppress the 30s-throttled diagnostics block
    return sampler


def _forced_reconnect(sampler):
    """Drive one error-saturation round: >20 consecutive errors, which disconnects
    the BMS and bumps the forced-reconnect count."""
    async def _sample_inner():
        raise TimeoutError("notify timeout")

    sampler._sample_inner = _sample_inner
    sampler.bms.is_connected = True
    sampler._t_wd_reset = time.time()  # not the "no data flowing" disconnect path
    sampler._num_errors = 20  # __call__ increments to 21, tripping the > 20 branch

    with pytest.raises(TimeoutError):  # __call__ re-raises; fetch_loop catches it
        asyncio.run(sampler())


def test_no_power_cycle_when_the_option_is_off(bt_power_calls):
    sampler = _make_sampler()  # bt_power_cycle_on_error defaults to False
    for _ in range(5):
        _forced_reconnect(sampler)
    assert bt_power_calls == []
    assert sampler._num_error_disconnects == 5  # counted, just never acted on


def test_power_cycle_after_repeated_forced_reconnects(bt_power_calls):
    sampler = _make_sampler(bt_power_cycle_on_error=True)

    _forced_reconnect(sampler)
    assert bt_power_calls == [], "must not cycle the radio on the first reconnect"

    _forced_reconnect(sampler)
    assert bt_power_calls == [False, True]
    assert sampler._num_error_disconnects == 0  # counter restarts after acting


def test_a_good_sample_prevents_escalation(bt_power_calls):
    """The BMS recovers between saturation rounds, so it must never escalate -
    otherwise a battery that merely drops out now and then cycles the radio."""
    sampler = _make_sampler(bt_power_cycle_on_error=True)

    async def _ok():
        return object()

    for _ in range(6):
        _forced_reconnect(sampler)
        assert sampler._num_error_disconnects == 1
        sampler._sample_inner = _ok
        asyncio.run(sampler())
        assert sampler._num_error_disconnects == 0

    assert bt_power_calls == []


def test_power_cycle_is_rate_limited_across_samplers(bt_power_calls, monkeypatch):
    """All samplers share one controller: three wedged BMS must not cycle it three
    times in a row."""
    monkeypatch.setattr(sampling, "BT_POWER_CYCLE_MIN_INTERVAL", 600)

    for _ in range(3):
        sampler = _make_sampler(bms=_FakeBms(), bt_power_cycle_on_error=True)
        _forced_reconnect(sampler)
        _forced_reconnect(sampler)

    assert bt_power_calls == [False, True], "rate limit did not hold"

    # ...and once the interval has elapsed, recovery is available again
    monkeypatch.setattr(sampling, "_t_last_bt_power_cycle", time.time() - 601)
    sampler = _make_sampler(bt_power_cycle_on_error=True)
    _forced_reconnect(sampler)
    _forced_reconnect(sampler)
    assert bt_power_calls == [False, True, False, True]


@pytest.mark.parametrize("attr,value", [("address", "serial"), ("is_virtual", True)])
def test_wired_and_virtual_bms_never_power_cycle_bluetooth(bt_power_calls, attr, value):
    """A wired RS485 BMS or a virtual group failing has nothing to do with the
    bluetooth controller."""
    bms = _FakeBms()
    setattr(bms, attr, value)
    sampler = _make_sampler(bms=bms, bt_power_cycle_on_error=True)

    for _ in range(5):
        _forced_reconnect(sampler)

    assert bt_power_calls == []


def test_power_cycle_survives_a_failing_bt_power(monkeypatch):
    """bt_power shells out to bluetoothctl, which may be absent (bumble/esphome) or
    fail. The sampler must keep going, and must not report success."""
    monkeypatch.setattr(sampling, "BT_POWER_CYCLE_SETTLE", 0)
    monkeypatch.setattr(sampling, "_t_last_bt_power_cycle", 0.0)

    def _boom(on):
        raise OSError("bluetoothctl not found")

    monkeypatch.setattr(bmslib.bt, "bt_power", _boom)
    assert asyncio.run(sampling.bt_power_cycle("fake")) is False
