"""JBD basic-info (cmd 0x03) decode regression tests."""

import asyncio
import math
import types

import pytest

import bmslib.bt as bt_module
from bmslib.models.jbd import (
    JbdBt,
    _decode_jbd_date,
    _decode_jbd_setting,
    _jbd_write_command,
)
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import jbd_fixtures


def _jbd_frame(jbd_current, charge_ah=100.0, capacity_ah=100.0, num_temp=0, balance=0):
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
    data += (balance & 0xFFFF).to_bytes(2, 'big')   # balance status cells 1-16
    data += (balance >> 16).to_bytes(2, 'big')       # balance status cells 17-32
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


def _jbd_voltage_frame(voltages):
    payload = b''.join(voltage.to_bytes(2, 'big') for voltage in voltages)
    frame = bytes([0xDD, 0x04, 0x00, len(payload)]) + payload
    checksum = (0x10000 - sum(frame[2:])) & 0xFFFF
    return frame + checksum.to_bytes(2, 'big') + bytes([0x77])


def _fetch_voltages(bms, frame):
    async def fake_q(*a, **kw):
        return frame
    bms._q = fake_q
    return asyncio.run(bms.fetch_voltages())


def test_jbd_voltage_decode_preserves_valid_zero_cell_reading():
    """A real zero in an intact, checksum-valid frame must never be hidden."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    assert _fetch_voltages(bms, _jbd_voltage_frame([3301, 0, 3299])) == [3301, 0, 3299]


def test_jbd_voltage_decode_rejects_truncated_frame_instead_of_fabricating_zeros():
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    complete = _jbd_voltage_frame([3300 + i for i in range(13)])
    # Retain the declared 26-byte payload length but simulate receipt ending
    # after cell 8. The old decoder returned five fabricated zero values.
    truncated = complete[:4 + 8 * 2] + bytes([0x77])

    with pytest.raises(ValueError, match="length mismatch"):
        _fetch_voltages(bms, truncated)


def test_jbd_decode_balancing_cells_bitmask():
    """#283: balance status words -> bitmask, bit 0 = cell 1, cells 17+ in the high word."""
    from bmslib.mqtt_util import balancing_cells_str
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    mask = (1 << 0) | (1 << 4) | (1 << 17)
    sample = run_fetch_with_response(bms, _jbd_frame(0.0, balance=mask))
    assert sample.balancing_cells == mask
    assert balancing_cells_str(sample.balancing_cells) == "1,5,18"
    assert run_fetch_with_response(bms, _jbd_frame(0.0)).balancing_cells == 0
    assert balancing_cells_str(0) == "none"


def test_jbd_decode_rejects_ntc_count_overrunning_payload():
    """#321: a frame whose NTC count byte exceeds the payload must not fabricate
    253 sensors at -273.1 C."""
    frame = bytearray(_jbd_frame(0.0, num_temp=3))
    frame[4 + 22] = 253  # num_temp byte, header is 4 bytes
    checksum = (0x10000 - sum(frame[2:-3])) & 0xFFFF
    frame[-3:-1] = checksum.to_bytes(2, 'big')
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    with pytest.raises(ValueError, match="253 NTC"):
        run_fetch_with_response(bms, bytes(frame))
    sample = run_fetch_with_response(bms, _jbd_frame(0.0, num_temp=3))
    assert len(sample.temperatures) == 3


def test_jbd_voltage_decode_rejects_bad_checksum():
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    frame = bytearray(_jbd_voltage_frame([3300, 3301]))
    frame[4] ^= 0x01

    with pytest.raises(ValueError, match="checksum mismatch"):
        _fetch_voltages(bms, bytes(frame))


def test_jbd_notification_waits_for_declared_frame_length_even_if_fragment_ends_in_77():
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    frame = _jbd_voltage_frame([3300 + i for i in range(13)])
    fragment = frame[:4 + 8 * 2]
    fragment = fragment[:-1] + bytes([0x77])  # terminator-like payload byte

    bms._notification_handler(None, fragment)

    assert bytes(bms._buffer) == fragment
    assert bms._last_response is None


def test_jbd_factory_mode_write_frames():
    assert _jbd_write_command(0x00, bytes.fromhex("5678")) == bytes.fromhex(
        "dd5a00025678ff3077"
    )


def test_jbd_extended_settings_decode():
    assert _decode_jbd_setting(0x10, bytes.fromhex("0514")) == 13.0
    assert _decode_jbd_setting(0x11, bytes.fromhex("04b0")) == 12.0
    assert _decode_jbd_setting(0x12, bytes.fromhex("1036")) == 4.15
    assert _decode_jbd_setting(0x20, bytes.fromhex("1554")) == 54.6
    assert _decode_jbd_setting(0x2A, bytes.fromhex("0f3c")) == 3.9
    assert _decode_jbd_setting(0xA0, bytes.fromhex("0656656e646f72")) == "Vendor"
    assert _decode_jbd_setting(0xA1, bytes.fromhex("074d6f64656c2031")) == "Model 1"


def test_jbd_manufacture_date_and_write_frames():
    assert _decode_jbd_date(0x3021) == "2024-01-01"
    assert _jbd_write_command(0xE1, bytes.fromhex("0002")) == bytes.fromhex(
        "dd5ae1020002ff1b77"
    )
    assert _jbd_write_command(0x01, bytes.fromhex("2828")) == bytes.fromhex(
        "dd5a01022828ffad77"
    )


def _switch_bms(states, connected=True):
    """JbdBt whose _fetch() reports `states` (list of (charge, discharge, problem_code)),
    one per call, the last repeating; _w records writes."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    bms.client = types.SimpleNamespace(is_connected=connected)
    bms._connect_complete = connected
    writes = []
    calls = iter(states)
    last = [states[0]]

    async def fake_w(command, data, timeout=None):
        writes.append((command, data))

    async def fake_fetch():
        last[0] = next(calls, last[0])
        c, d, p = last[0]
        return types.SimpleNamespace(switches={"charge": c, "discharge": d}, problem_code=p)

    bms._w = fake_w
    bms._fetch = fake_fetch
    return bms, writes


SEQ_CHARGE_ON_DISCHARGE_OFF = [
    (0x00, bytes.fromhex("5678")),
    (0xE1, bytes.fromhex("0002")),
    (0x01, bytes.fromhex("2828")),
]


def test_jbd_charge_on_uses_write_mode_and_preserves_discharge_off():
    bms, writes = _switch_bms([(False, False, 0), (True, False, 0)])
    asyncio.run(bms.set_switch("charge", True))
    assert writes == SEQ_CHARGE_ON_DISCHARGE_OFF


def test_jbd_switch_verification_reports_rejected_write():
    bms, _ = _switch_bms([(False, False, 4096)])
    with pytest.raises(RuntimeError, match="problem_code=4096"):
        asyncio.run(bms.set_switch("charge", True))


def test_jbd_retained_command_for_the_current_state_is_not_written():
    bms, writes = _switch_bms([(True, True, 0)])
    asyncio.run(bms.set_switch("charge", True))
    assert writes == []


def test_jbd_command_held_by_a_protection_is_written_once():
    # over-temperature holds charge off; the user's "off" must still be written,
    # or charge comes back when the protection clears
    bms, writes = _switch_bms([(False, True, 0x0100)])
    asyncio.run(bms.set_switch("charge", False))
    assert [w[0] for w in writes] == [0x00, 0xE1, 0x01]
    asyncio.run(bms.set_switch("charge", False))
    assert len(writes) == 3


def test_jbd_refused_write_is_not_repeated_every_sample():
    # the algorithm asks for charge on every sample while a protection holds it
    bms, writes = _switch_bms([(False, True, 4096)])
    with pytest.raises(RuntimeError, match="problem_code"):
        asyncio.run(bms.set_switch("charge", True))
    asyncio.run(bms.set_switch("charge", True))  # within SWITCH_REWRITE_S: skipped
    asyncio.run(bms.set_switch("charge", True))
    assert len(writes) == 3
    bms._written["charge"] = (True, bms._written["charge"][1] - bms.SWITCH_REWRITE_S - 1)
    with pytest.raises(RuntimeError, match="problem_code"):
        asyncio.run(bms.set_switch("charge", True))
    assert len(writes) == 6


def test_jbd_other_fet_keeps_the_commanded_state_not_the_protected_one():
    # discharge was commanded on, a protection turned it off; switching charge
    # must not latch discharge off
    bms, writes = _switch_bms([(False, False, 0x0002), (True, False, 0x0002)])
    bms._commanded["discharge"] = True
    asyncio.run(bms.set_switch("charge", True))
    assert writes[1] == (0xE1, bytes.fromhex("0000"))


def test_jbd_switch_while_disconnected_is_applied_by_the_next_fetch():
    bms, writes = _switch_bms([(False, False, 0), (True, False, 0)], connected=False)
    asyncio.run(bms.set_switch("charge", True))
    assert writes == []
    sample = asyncio.run(bms.fetch())
    assert writes == SEQ_CHARGE_ON_DISCHARGE_OFF
    assert sample.switches["charge"] is True


def test_jbd_pending_switch_retries_one_timed_out_status_read():
    bms, writes = _switch_bms([(False, False, 0), (True, False, 0)], connected=False)
    real_fetch = bms._fetch
    calls = 0

    async def flaky_fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError()
        return await real_fetch()

    bms._fetch = flaky_fetch
    asyncio.run(bms.set_switch("charge", True))
    sample = asyncio.run(bms.fetch())

    assert calls == 3  # timed-out status, retry, then post-write verification
    assert writes == SEQ_CHARGE_ON_DISCHARGE_OFF
    assert sample.switches["charge"] is True
    assert bms._pending_switches == {}


def test_jbd_pending_switch_survives_two_timed_out_status_reads():
    bms, writes = _switch_bms([(False, False, 0)], connected=False)

    async def timed_out_fetch():
        raise asyncio.TimeoutError()

    bms._fetch = timed_out_fetch
    asyncio.run(bms.set_switch("charge", True))
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.fetch())

    assert writes == []
    assert bms._pending_switches == {"charge": True}


def _fetch(bms, frame):
    """fetch() then the sampler's runtime derivation, mirroring BmsSampler.

    runtime is no longer computed in fetch(); the sampler calls
    bms.estimate_runtime(sample) once per poll. Reproduce that here so the
    per-poll cadence/EWMA state advances exactly as it does in production."""
    async def fake_q(*a, **kw):
        return frame
    bms._q = fake_q
    sample = asyncio.run(bms.fetch())
    sample.runtime = bms.estimate_runtime(sample)
    return sample


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
    # runtime is derived by the sampler, not fetch(); replicate that one step.
    sample.runtime = bms.estimate_runtime(sample)
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


class _FakeTime:
    """Stand-in for the `time` module with independently movable clocks, so a test
    can step the wall clock backward while monotonic keeps advancing."""

    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, dt):
        self.mono += dt
        self.wall += dt


@pytest.fixture
def fake_clock(monkeypatch):
    """A controllable clock for the runtime estimator (BtBms.estimate_runtime,
    in bmslib.bt), so tests can simulate polling cadence and outages without
    sleeping.

    Replaces the *name* `time` in bt's namespace. Patching `bmslib.bt.time.
    monotonic` instead would mutate the stdlib `time` module process-wide
    (`bt.time is sys.modules['time']`), and asyncio's event loop reads
    `time.monotonic()` for every scheduling deadline -- a frozen clock makes
    `asyncio.wait_for` never fire, hanging the suite instead of failing it.
    """
    clock = _FakeTime()
    monkeypatch.setattr(bt_module, "time", clock)
    return clock


def test_jbd_runtime_ewma_resets_after_a_long_gap(fake_clock):
    """EWMA is sample-indexed, not time-aware: without this reset a sample from
    an hour ago would blend into the first post-outage estimate as if it were
    one poll old. Reviewer's scenario for #381."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    charge_ah = 50.0

    # steady 60 s polling at a 10 A load
    for _ in range(20):
        fake_clock.advance(60)
        s = _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=charge_ah))
    assert s.runtime == pytest.approx(charge_ah / 10.0 * 3600, rel=0.001)

    # BLE drops for an hour; when it returns the real load is only 2 A
    fake_clock.advance(3600)
    s = _fetch(bms, _jbd_frame(jbd_current=-2.0, charge_ah=charge_ah))

    # the filter is reseeded, so the estimate reflects 2 A, not a blend with 10 A
    assert s.runtime == pytest.approx(charge_ah / 2.0 * 3600, rel=0.001)


def test_jbd_runtime_ewma_survives_cadence_jitter(fake_clock):
    """A slow poll or a bit of scheduler jitter must not be mistaken for an
    outage, or the EWMA degenerates into the instantaneous current."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    charge_ah = 100.0

    for _ in range(6):
        fake_clock.advance(60)
        _fetch(bms, _jbd_frame(jbd_current=-2.0, charge_ah=charge_ah))

    fake_clock.advance(75)  # 25% late, well inside 4x
    s = _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=charge_ah))

    blended = (1 - 2 / 7) * 2.0 + (2 / 7) * 10.0
    assert s.runtime == pytest.approx(charge_ah / blended * 3600, rel=0.01)


def test_jbd_runtime_ewma_fast_poll_jitter_is_not_an_outage(fake_clock):
    """At a fast poll, 4x the cadence is a tiny absolute gap; the floor keeps
    ordinary jitter from resetting the filter every other sample."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")

    for _ in range(6):
        fake_clock.advance(1.0)
        _fetch(bms, _jbd_frame(jbd_current=-2.0, charge_ah=100.0))

    fake_clock.advance(6.0)  # 6x the 1 s cadence, but under EWMA_STALE_MIN_S
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
        fake_clock.advance(gap)
        return _fetch(bms, _jbd_frame(jbd_current=-amps, charge_ah=50.0))

    for _ in range(8):
        poll(60, 10.0)                   # settle: cadence 60 s, current 10 A
    poll(3600, 2.0)                      # outage -> reseed to 2 A
    assert bms._runtime_current_ewma.value == pytest.approx(2.0, rel=0.001)

    poll(60, 2.0)                        # one normal poll relearns cadence = 60 s
    assert bms._runtime_sample_dt.value == pytest.approx(60.0, rel=0.001)

    s = poll(1200, 5.0)                  # second outage -> reseed to 5 A, not blended
    assert bms._runtime_current_ewma.value == pytest.approx(5.0, rel=0.001)
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
    """One sampler cycle: `async with bms:` (connect per policy) then fetch.

    Each call spins a fresh event loop. That is safe only while bt.ConnectLock is
    uncontended -- an asyncio.Lock binds to a loop on its first contended acquire
    and then raises against any other loop. Don't gather concurrent _poll() calls.
    """
    async def fake_q(*a, **kw):
        return frame

    bms._q = fake_q

    async def run():
        async with bms:
            return await bms.fetch()

    sample = asyncio.run(run())
    sample.runtime = bms.estimate_runtime(sample)
    return sample


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
        fake_clock.advance(60)
        _poll(bms, _jbd_frame(jbd_current=-2.0, charge_ah=100.0))

    assert len(fake_ble.connects) == 6, "keep_alive=False must reconnect every poll"
    assert bms.client.is_connected is False, "__aexit__ must disconnect after each poll"
    assert bms._runtime_current_ewma.value == pytest.approx(2.0, rel=0.01)

    fake_clock.advance(60)
    s = _poll(bms, _jbd_frame(jbd_current=-10.0, charge_ah=100.0))

    blended = (1 - 2 / 7) * 2.0 + (2 / 7) * 10.0
    assert bms._runtime_current_ewma.value == pytest.approx(blended, rel=0.01), \
        "connect() reseeded the filter; smoothing is dead under keep_alive=false"
    assert s.runtime == pytest.approx(100.0 / blended * 3600, rel=0.01)


def test_jbd_keep_alive_true_connects_once_and_smooths(fake_clock, fake_ble):
    """The counterpart: with keep_alive the connection is held across polls."""
    bms = fake_ble.make(keep_alive=True)

    for _ in range(6):
        fake_clock.advance(60)
        _poll(bms, _jbd_frame(jbd_current=-2.0, charge_ah=100.0))

    assert len(fake_ble.connects) == 1, "keep_alive=True must hold the connection"
    assert bms.client.is_connected is True
    assert bms._runtime_current_ewma.value == pytest.approx(2.0, rel=0.01)


def test_jbd_real_ble_drop_reconnects_and_resets_the_filter(fake_clock, fake_ble):
    """A genuine drop: the client goes disconnected, __aenter__ reconnects on the
    next poll, and because a real gap elapsed the stale current is discarded.

    This moves two variables at once (reconnect + gap). The attribution is pinned
    by its companions: keep_alive_false reconnects every poll with NO gap and the
    filter keeps blending, and resets_after_a_long_gap has a gap with NO reconnect
    and the filter resets. So the reset here is caused by elapsed time, not connect.
    """
    bms = fake_ble.make(keep_alive=True)

    for _ in range(8):
        fake_clock.advance(60)
        _poll(bms, _jbd_frame(jbd_current=-10.0, charge_ah=50.0))
    assert bms._runtime_current_ewma.value == pytest.approx(10.0, rel=0.001)

    bms.client.is_connected = False      # BLE drops; an hour passes
    fake_clock.advance(3600)
    s = _poll(bms, _jbd_frame(jbd_current=-2.0, charge_ah=50.0))

    assert len(fake_ble.connects) == 2, "__aenter__ must reconnect after a drop"
    assert bms._runtime_current_ewma.value == pytest.approx(2.0, rel=0.001), \
        "stale pre-outage current survived the reconnect"
    assert s.runtime == pytest.approx(50.0 / 2.0 * 3600, rel=0.001)


def test_aenter_keep_alive_check_is_independent_of_aexit(fake_ble):
    """__aenter__ skips connecting only when BOTH keep_alive and is_connected.

    Every other test enters and exits in pairs, and __aexit__ already disconnects
    whenever keep_alive is false -- so `is_connected` alone would give identical
    results and the `keep_alive and` half of the condition goes unpinned. Enter
    twice without exiting to isolate it.
    """
    ka = fake_ble.make(keep_alive=True)
    asyncio.run(ka.__aenter__())
    asyncio.run(ka.__aenter__())
    assert len(fake_ble.connects) == 1, "keep_alive=True must reuse a live connection"

    fake_ble.connects.clear()
    no_ka = fake_ble.make(keep_alive=False)
    asyncio.run(no_ka.__aenter__())
    assert no_ka.client.is_connected is True
    asyncio.run(no_ka.__aenter__())   # still connected, but keep_alive is off
    assert len(fake_ble.connects) == 2, \
        "keep_alive=False must reconnect even when the client is already connected"


def test_jbd_backward_wall_clock_step_does_not_disturb_the_filter(fake_clock):
    """An NTP correction stepping time.time() backward (a Pi with no RTC) must not
    poison the learned cadence. estimate_runtime reads a monotonic clock.

    If it read the wall clock, the step would make dt negative (-200 + 60 = -140s),
    dragging typical_dt from 60s to 2.9s. The spurious reset then lands on the
    *following* ordinary poll, whose 60s dt exceeds the collapsed 11.4s threshold --
    so the check has to look one poll further than the step itself.
    """
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    for _ in range(8):
        fake_clock.advance(60)   # advances wall and monotonic together
        _fetch(bms, _jbd_frame(jbd_current=-10.0, charge_ah=100.0))
    assert bms._runtime_sample_dt.value == pytest.approx(60.0, rel=0.001)

    fake_clock.wall -= 200       # NTP steps the wall clock back; monotonic cannot
    fake_clock.advance(60)       # 60 s of real time passes before the next poll
    _fetch(bms, _jbd_frame(jbd_current=-14.0, charge_ah=100.0))

    assert bms._runtime_sample_dt.value == pytest.approx(60.0, rel=0.01), \
        "a negative interval reached the cadence filter; it is not reading monotonic"
    blend1 = (1 - 2 / 7) * 10.0 + (2 / 7) * 14.0
    assert bms._runtime_current_ewma.value == pytest.approx(blend1, rel=0.001)

    fake_clock.advance(60)       # the poll where a collapsed threshold would bite
    s = _fetch(bms, _jbd_frame(jbd_current=-14.0, charge_ah=100.0))

    blend2 = (1 - 2 / 7) * blend1 + (2 / 7) * 14.0
    assert bms._runtime_current_ewma.value == pytest.approx(blend2, rel=0.001), \
        "backward clock step spuriously reset the filter one poll later"
    assert s.runtime == pytest.approx(100.0 / blend2 * 3600, rel=0.01)


def _reply(cmd: int, payload: bytes) -> bytes:
    body = bytes([0x00, len(payload)]) + payload
    return bytes([0xDD, cmd]) + body + ((0x10000 - sum(body)) & 0xFFFF).to_bytes(2, 'big') + b"\x77"


def _device_info_bms(answers, enter_ok=True):
    """JbdBt whose _q answers from `answers` (cmd -> payload) and times out otherwise."""
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    writes, queries = [], []

    async def fake_w(command, data, timeout=None):
        writes.append((command, data))
        if command == 0x00 and not enter_ok:
            raise asyncio.TimeoutError()

    async def fake_q(command, timeout=None):
        queries.append(command)
        if command == 0x03:
            return jbd_fixtures.SYSSI_3CELL["raw"]
        if command in answers:
            return _reply(command, answers[command])
        raise asyncio.TimeoutError()

    bms._w = fake_w
    bms._q = fake_q
    return bms, writes, queries


def test_jbd_device_info_leaves_factory_mode_without_saving():
    bms, writes, _ = _device_info_bms({0x10: bytes.fromhex("0514"), 0x16: bytes.fromhex("0001"),
                                       0xA1: bytes.fromhex("074d6f64656c2031")})
    bms.SETTING_REGISTERS = [0x10, 0x16, 0xA1]
    di = asyncio.run(bms.fetch_device_info())
    # 28 28 would save to EEPROM; a read must exit with 00 00
    assert writes == [(0x00, bytes.fromhex("5678")), (0x01, bytes.fromhex("0000"))]
    assert di.values["jbd/design_capacity"] == 13.0
    assert di.model == "Model 1"
    # clones share factory serials: never the HA device identifier
    assert di.sn is None


def test_jbd_device_info_stops_after_unanswered_registers():
    bms, writes, queries = _device_info_bms({})
    di = asyncio.run(bms.fetch_device_info())
    assert queries == bms.SETTING_REGISTERS[:bms.SETTING_MAX_MISSES] + [0x03]
    assert writes[-1] == (0x01, bytes.fromhex("0000"))
    assert di.values == {}


def test_jbd_device_info_without_factory_mode_still_returns_basic_info():
    bms, writes, queries = _device_info_bms({}, enter_ok=False)
    di = asyncio.run(bms.fetch_device_info())
    assert queries == [0x03]
    # the ack may be lost while the BMS entered: leave without saving anyway
    assert writes == [(0x00, bytes.fromhex("5678")), (0x01, bytes.fromhex("0000"))]
    assert di.sw_version == "8.0"


def test_jbd_sampler_disconnect_waits_for_a_switch_write_in_flight():
    # MQTT switch commands run concurrently with the sampler; a disconnect
    # between factory-mode enter and exit would leave the BMS in factory mode
    bms, writes = _switch_bms([(False, False, 0), (True, False, 0)])
    bms.keep_alive = False
    events = []
    gate = asyncio.Event()

    async def slow_w(command, data, timeout=None):
        writes.append((command, data))
        if command == 0x00:
            await gate.wait()

    async def fake_disconnect():
        events.append(("disconnect", len(writes)))

    bms._w = slow_w
    bms.disconnect = fake_disconnect

    async def run():
        task = asyncio.create_task(bms.set_switch("charge", True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        exit_task = asyncio.create_task(bms.__aexit__(None, None, None))
        await asyncio.sleep(0.01)
        assert events == []  # still waiting for the write sequence
        gate.set()
        await task
        await exit_task

    asyncio.run(run())
    assert events == [("disconnect", 3)]


def test_jbd_refused_write_reaches_the_waiter_not_a_timeout():
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    body = bytes([0x80, 0x00])  # status 0x80: refused
    refusal = bytes([0xDD, 0xE1]) + body + ((0x10000 - sum(body)) & 0xFFFF).to_bytes(2, 'big') + b"\x77"

    class Client:
        async def write_gatt_char(self, uuid, data):
            bms._notification_handler(None, bytearray(refusal))

    bms.client = Client()
    with pytest.raises(ValueError, match="failed with status 0x80"):
        asyncio.run(bms._w(0xE1, bytes.fromhex("0002"), timeout=0.5))


def test_jbd_failed_write_is_retried_at_once():
    bms, writes = _switch_bms([(False, True, 0)])
    calls = []

    async def failing_enter(command, data, timeout=None):
        calls.append(command)
        if command == 0x00:
            raise asyncio.TimeoutError()

    bms._w = failing_enter
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.set_switch("charge", True))
    assert "charge" not in bms._written
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.set_switch("charge", True))
    assert calls == [0x00, 0x00]


def test_jbd_command_changed_elsewhere_without_protection_is_written_again():
    # HA turned charge on, then the vendor app turned it off: a new "on" from
    # HA within SWITCH_REWRITE_S must still be written
    bms, writes = _switch_bms([(False, True, 0), (True, True, 0), (False, True, 0), (True, True, 0)])
    asyncio.run(bms.set_switch("charge", True))
    asyncio.run(bms.set_switch("charge", True))
    assert [w[0] for w in writes] == [0x00, 0xE1, 0x01] * 2


def test_jbd_command_queued_when_the_sampler_disconnected_meanwhile():
    bms, writes = _switch_bms([(False, True, 0), (True, True, 0)])

    async def run():
        async with bms._io_lock:  # the sampler's __aexit__ holds the lock ...
            task = asyncio.create_task(bms.set_switch("charge", True))
            await asyncio.sleep(0)
            bms.client.is_connected = False  # ... and disconnects
            bms._connect_complete = False
        await task

    asyncio.run(run())
    assert writes == [] and bms._pending_switches == {"charge": True}


def test_jbd_periodic_reconnect_waits_for_a_switch_write_in_flight():
    bms, writes = _switch_bms([(False, False, 0), (True, False, 0)])
    events = []
    gate = asyncio.Event()

    async def slow_w(command, data, timeout=None):
        writes.append((command, data))
        if command == 0x00:
            await gate.wait()

    async def fake_force_disconnect(timeout=None):
        events.append(len(writes))

    bms._w = slow_w
    bms._force_disconnect = fake_force_disconnect

    async def run():
        task = asyncio.create_task(bms.set_switch("charge", True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cut = asyncio.create_task(bms.force_disconnect())
        await asyncio.sleep(0.01)
        assert events == []
        gate.set()
        await task
        await cut

    asyncio.run(run())
    assert events == [3]
