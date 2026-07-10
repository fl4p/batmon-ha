"""JBD basic-info (cmd 0x03) decode regression tests."""

import asyncio
import math

import pytest

from bmslib.models.jbd import JbdBt
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import jbd_fixtures


def _jbd_frame(jbd_current, charge_ah=100.0, capacity_ah=100.0, num_temp=0):
    """Build a minimal JBD 0x03 response frame.

    jbd_current uses JBD wire sign (positive=charging, negative=discharging);
    the decoder negates it to batmon convention (positive=discharging).
    """
    data = bytearray()
    data += int(26.0 * 100).to_bytes(2, 'big')
    data += int(jbd_current * 100).to_bytes(2, 'big', signed=True)
    data += int(charge_ah * 100).to_bytes(2, 'big')
    data += int(capacity_ah * 100).to_bytes(2, 'big')
    data += (0).to_bytes(2, 'big')       # cycles
    data += (0).to_bytes(2, 'big')       # production date
    data += (0).to_bytes(4, 'big')       # balancer
    data += (0).to_bytes(2, 'big')       # protection
    data += bytes([0x80])                # version
    data += bytes([100])                 # SOC
    data += bytes([0x03])                # MOS
    data += bytes([4])                   # num_cell
    data += bytes([num_temp])            # num_temp
    for _ in range(num_temp):
        data += (2731).to_bytes(2, 'big')
    frame = bytes([0xDD, 0x03, 0x00, len(data)]) + data
    checksum = (0x10000 - sum(frame[2:])) & 0xFFFF
    frame += checksum.to_bytes(2, 'big') + bytes([0x77])
    return frame


def _fetch(bms, frame):
    async def fake_q(*a, **kw):
        return frame
    bms._q = fake_q
    return asyncio.run(bms.fetch())


@pytest.mark.parametrize("fx", jbd_fixtures.ALL, ids=lambda fx: fx["name"])
def test_jbd_decode(fx):
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    sample = run_fetch_with_response(bms, fx["raw"])
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.01)
    assert sample.current == pytest.approx(exp["current"], abs=0.01)
    assert sample.charge == pytest.approx(exp["charge"], abs=0.01)
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.01)
    if "soc" in exp:
        assert sample.soc == pytest.approx(exp["soc"], abs=0.1)
    assert sample.num_cycles == exp["num_cycles"]
    assert sample.temperatures == pytest.approx(exp["temperatures"], abs=0.1)
    assert sample.switches == exp["switches"]
    if "problem_code" in exp:
        assert sample.problem_code == exp["problem_code"]
    if "problem" in exp:
        assert sample.problem == exp["problem"]
    if exp.get("runtime") is None:
        assert math.isnan(sample.runtime)
    else:
        assert sample.runtime == pytest.approx(exp["runtime"], rel=0.01)


def test_jbd_runtime_ewma_charge_discharge_transition():
    """Runtime EWMA resets on charge/idle so a charge→discharge transition
    seeds fresh instead of being contaminated by stale negative values."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    charge_ah = 100.0

    # 1. Charging at 10 A (JBD wire: +1000) → batmon current = -10 → runtime nan
    s = _fetch(bms, _jbd_frame(jbd_current=10.0, charge_ah=charge_ah))
    assert math.isnan(s.runtime)

    # 2. First discharge at 5 A (JBD wire: -500) → EWMA seeds to 5.0 (no blending
    #    on first sample) → runtime = 100/5*3600 = 72000 s exactly
    s = _fetch(bms, _jbd_frame(jbd_current=-5.0, charge_ah=charge_ah))
    assert s.runtime == pytest.approx(72000, rel=0.001)

    # 3. Second discharge at 10 A → EWMA blends: (1-α)*5 + α*10
    #    α = 2/(6+1) ≈ 0.2857 → smoothed ≈ 6.4286 → runtime = 100/6.4286*3600
    s = _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=charge_ah))
    expected_current = (1 - 2/7) * 5.0 + (2/7) * 10.0
    assert s.runtime == pytest.approx(charge_ah / expected_current * 3600, rel=0.01)

    # 4. Charging again → runtime nan, EWMA resets
    s = _fetch(bms, _jbd_frame(jbd_current=10.0, charge_ah=charge_ah))
    assert math.isnan(s.runtime)

    # 5. Discharge at 8 A → EWMA seeds fresh to 8.0 (not contaminated by step 3)
    s = _fetch(bms, _jbd_frame(jbd_current=-8.0, charge_ah=charge_ah))
    assert s.runtime == pytest.approx(charge_ah / 8.0 * 3600, rel=0.001)


@pytest.fixture
def fake_clock(monkeypatch):
    """A controllable time.time() for bmslib.models.jbd, so tests can simulate
    polling cadence and outages without sleeping."""
    now = [1000.0]
    monkeypatch.setattr("bmslib.models.jbd.time.time", lambda: now[0])
    return now


def test_jbd_runtime_ewma_resets_after_a_long_gap(fake_clock):
    """EWMA is sample-indexed, not time-aware: without this reset a sample from
    an hour ago would blend into the first post-outage estimate as if it were
    one poll old. Reviewer's scenario for #381."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    charge_ah = 50.0

    # steady 60 s polling at a 10 A load
    for _ in range(20):
        fake_clock[0] += 60
        s = _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=charge_ah))
    assert s.runtime == pytest.approx(charge_ah / 10.0 * 3600, rel=0.001)

    # BLE drops for an hour; when it returns the real load is only 2 A
    fake_clock[0] += 3600
    s = _fetch(bms, _jbd_frame(jbd_current=-2.0, charge_ah=charge_ah))

    # the filter is reseeded, so the estimate reflects 2 A, not a blend with 10 A
    assert s.runtime == pytest.approx(charge_ah / 2.0 * 3600, rel=0.001)


def test_jbd_runtime_ewma_survives_cadence_jitter(fake_clock):
    """A slow poll or a bit of scheduler jitter must not be mistaken for an
    outage, or the EWMA degenerates into the instantaneous current."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    charge_ah = 100.0

    for _ in range(6):
        fake_clock[0] += 60
        _fetch(bms, _jbd_frame(jbd_current=-2.0, charge_ah=charge_ah))

    fake_clock[0] += 75  # 25% late, well inside 4x
    s = _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=charge_ah))

    blended = (1 - 2 / 7) * 2.0 + (2 / 7) * 10.0
    assert s.runtime == pytest.approx(charge_ah / blended * 3600, rel=0.01)


def test_jbd_runtime_ewma_fast_poll_jitter_is_not_an_outage(fake_clock):
    """At a fast poll, 4x the cadence is a tiny absolute gap; the floor keeps
    ordinary jitter from resetting the filter every other sample."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")

    for _ in range(6):
        fake_clock[0] += 1.0
        _fetch(bms, _jbd_frame(jbd_current=-2.0, charge_ah=100.0))

    fake_clock[0] += 6.0  # 6x the 1 s cadence, but under EWMA_STALE_MIN_S
    s = _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=100.0))

    blended = (1 - 2 / 7) * 2.0 + (2 / 7) * 10.0
    assert s.runtime == pytest.approx(100.0 / blended * 3600, rel=0.01)


def test_jbd_reconnect_discards_the_previous_session_filter(fake_clock):
    """A new BLE session says nothing about the load before the old one ended."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")

    for _ in range(10):
        fake_clock[0] += 60
        _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=50.0))
    assert bms._current_ewma.value == pytest.approx(10.0, rel=0.001)

    bms._reset_current_ewma()
    assert math.isnan(bms._current_ewma.value)

    fake_clock[0] += 60
    s = _fetch(bms, _jbd_frame(jbd_current=-2.0, charge_ah=50.0))
    assert s.runtime == pytest.approx(50.0 / 2.0 * 3600, rel=0.001)


def test_jbd_connect_resets_the_current_filter(monkeypatch):
    """connect() must drop the previous session's smoothed current."""
    from bmslib.bt import BtBms

    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    bms._current_ewma.add(10.0)
    assert bms._current_ewma.value == pytest.approx(10.0)

    async def noop_connect(self, **kwargs):
        pass

    class _Client:
        async def start_notify(self, *a, **kw):
            pass

    monkeypatch.setattr(BtBms, "connect", noop_connect)
    bms.client = _Client()
    asyncio.run(bms.connect())

    assert math.isnan(bms._current_ewma.value)
