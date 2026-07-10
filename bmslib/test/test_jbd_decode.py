"""JBD basic-info (cmd 0x03) decode regression tests."""

import asyncio
import math
import types

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
    """A controllable monotonic clock for bmslib.models.jbd, so tests can simulate
    polling cadence and outages without sleeping."""
    now = [1000.0]
    monkeypatch.setattr("bmslib.models.jbd.time.monotonic", lambda: now[0])
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


def test_jbd_second_outage_is_still_detected(fake_clock):
    """The stale gap must not be fed to the cadence filter: that would inflate the
    learned interval (60s -> ~780s) and blind the detector to the next outage.

    The sizes matter. A second 3600s gap would trip even a poisoned threshold, so
    this uses a 1200s gap after a single relearn poll — caught with a correctly
    reset cadence (threshold 240s), missed with a poisoned one (threshold ~3130s).
    """
    bms = JbdBt("00:11:22:33:44:55", name="jbd")

    def poll(gap, amps):
        fake_clock[0] += gap
        return _fetch(bms, _jbd_frame(jbd_current=-amps, charge_ah=50.0))

    for _ in range(8):
        poll(60, 10.0)                   # settle: cadence 60 s, current 10 A
    poll(3600, 2.0)                      # outage -> reseed to 2 A
    assert bms._current_ewma.value == pytest.approx(2.0, rel=0.001)

    poll(60, 2.0)                        # one normal poll relearns cadence = 60 s
    assert bms._sample_dt.value == pytest.approx(60.0, rel=0.001)

    s = poll(1200, 5.0)                  # second outage -> reseed to 5 A, not blended
    assert bms._current_ewma.value == pytest.approx(5.0, rel=0.001)
    assert s.runtime == pytest.approx(50.0 / 5.0 * 3600, rel=0.001)


class _FakeClient:
    """Minimal stand-in for a BleakClient. `services` stays empty so BtBms.stop_notify
    short-circuits without needing a GATT table."""

    def __init__(self):
        self.is_connected = False
        self.services = []

    async def start_notify(self, *a, **kw):
        pass

    async def disconnect(self):
        self.is_connected = False


@pytest.fixture
def fake_ble(monkeypatch):
    """Drive the real BtBms.__aenter__/__aexit__ and JbdBt.connect()/disconnect()
    against a fake client. Only the transport (_connect_client) is stubbed, so the
    keep_alive policy under test is the production one, not a copy of it."""
    from bmslib.bt import BtBms

    connects = []

    async def fake_connect_client(self, timeout=20):
        connects.append(1)
        self.client.is_connected = True

    monkeypatch.setattr(BtBms, "_connect_client", fake_connect_client)

    def make(**kwargs):
        bms = JbdBt("00:11:22:33:44:55", name="jbd", **kwargs)
        bms.client = _FakeClient()
        return bms

    return types.SimpleNamespace(make=make, connects=connects)


def _poll(bms, frame):
    """One sampler cycle: `async with bms:` (connect per policy) then fetch."""
    async def fake_q(*a, **kw):
        return frame

    bms._q = fake_q

    async def run():
        async with bms:
            return await bms.fetch()

    return asyncio.run(run())


def test_jbd_keep_alive_false_reconnects_every_poll_but_keeps_the_filter(fake_clock, fake_ble):
    """With `keep_alive: false` BtBms.__aenter__ connects before every poll and
    __aexit__ disconnects after it. Resetting the filter on connect() would leave
    runtime equal to the instantaneous current, i.e. no smoothing at all. Only
    elapsed time may reset it.

    This drives the real context manager, so it stays honest if __aenter__'s policy
    changes -- unlike a test that imitates it by calling connect() in a loop.
    """
    bms = fake_ble.make(keep_alive=False)

    for _ in range(6):                   # settle at 2 A across per-poll reconnects
        fake_clock[0] += 60
        _poll(bms, _jbd_frame(jbd_current=-2.0, charge_ah=100.0))

    assert len(fake_ble.connects) == 6, "keep_alive=False must reconnect every poll"
    assert bms.client.is_connected is False, "__aexit__ must disconnect after each poll"
    assert bms._current_ewma.value == pytest.approx(2.0, rel=0.01)

    fake_clock[0] += 60
    s = _poll(bms, _jbd_frame(jbd_current=-10.0, charge_ah=100.0))

    blended = (1 - 2 / 7) * 2.0 + (2 / 7) * 10.0
    assert bms._current_ewma.value == pytest.approx(blended, rel=0.01), \
        "connect() reseeded the filter; smoothing is dead under keep_alive=false"
    assert s.runtime == pytest.approx(100.0 / blended * 3600, rel=0.01)


def test_jbd_keep_alive_true_connects_once_and_smooths(fake_clock, fake_ble):
    """The counterpart: with keep_alive the connection is held across polls."""
    bms = fake_ble.make(keep_alive=True)

    for _ in range(6):
        fake_clock[0] += 60
        _poll(bms, _jbd_frame(jbd_current=-2.0, charge_ah=100.0))

    assert len(fake_ble.connects) == 1, "keep_alive=True must hold the connection"
    assert bms.client.is_connected is True
    assert bms._current_ewma.value == pytest.approx(2.0, rel=0.01)


def test_jbd_real_ble_drop_reconnects_and_resets_the_filter(fake_clock, fake_ble):
    """A genuine drop: the client goes disconnected, __aenter__ reconnects on the
    next poll, and because a real gap elapsed the stale current is discarded."""
    bms = fake_ble.make(keep_alive=True)

    for _ in range(8):
        fake_clock[0] += 60
        _poll(bms, _jbd_frame(jbd_current=-10.0, charge_ah=50.0))
    assert bms._current_ewma.value == pytest.approx(10.0, rel=0.001)

    bms.client.is_connected = False      # BLE drops; an hour passes
    fake_clock[0] += 3600
    s = _poll(bms, _jbd_frame(jbd_current=-2.0, charge_ah=50.0))

    assert len(fake_ble.connects) == 2, "__aenter__ must reconnect after a drop"
    assert bms._current_ewma.value == pytest.approx(2.0, rel=0.001), \
        "stale pre-outage current survived the reconnect"
    assert s.runtime == pytest.approx(50.0 / 2.0 * 3600, rel=0.001)


def test_jbd_backward_wall_clock_step_does_not_disturb_the_filter(fake_clock, monkeypatch):
    """An NTP correction stepping time.time() backward (a Pi with no RTC) must not
    poison the learned cadence. _age_current_ewma uses a monotonic clock."""
    wall = [1_000_000.0]
    monkeypatch.setattr("bmslib.models.jbd.time.time", lambda: wall[0])

    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    for _ in range(8):
        fake_clock[0] += 60
        wall[0] += 60
        _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=100.0))
    assert bms._sample_dt.value == pytest.approx(60.0, rel=0.001)

    wall[0] -= 200          # clock steps backward; monotonic keeps advancing
    fake_clock[0] += 60
    s = _fetch(bms, _jbd_frame(jbd_current=-14.0, charge_ah=100.0))

    assert bms._sample_dt.value > 0, "negative interval poisoned the cadence filter"
    blended = (1 - 2 / 7) * 10.0 + (2 / 7) * 14.0
    assert bms._current_ewma.value == pytest.approx(blended, rel=0.001), \
        "backward clock step spuriously reset the filter"
    assert s.runtime == pytest.approx(100.0 / blended * 3600, rel=0.01)
