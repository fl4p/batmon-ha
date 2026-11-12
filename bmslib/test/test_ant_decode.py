"""ANT BMS BLE decode regression tests."""

import math

import pytest

from bmslib.models.ant import AntBt
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import ant_fixtures


@pytest.mark.parametrize("fx", ant_fixtures.ALL, ids=lambda fx: fx["name"])
def test_ant_decode(fx):
    bms = AntBt("00:11:22:33:44:55", name="ant")
    sample = run_fetch_with_response(bms, fx["raw"])
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.05)
    assert sample.current == pytest.approx(exp["current"], abs=0.05)
    assert sample.charge == pytest.approx(exp["charge"], abs=0.05)
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.05)
    assert sample.total_charge_throughput == pytest.approx(exp["total_charge_throughput"], abs=0.01)
    assert sample.soc == pytest.approx(exp["soc"], abs=0.5)
    assert sample.soh == pytest.approx(exp["soh"], abs=0.5)
    assert sample.mos_temperature == exp["mos_temperature"]
    assert sample.switches == exp["switches"]

    # ANT exposes 65496 (== 0xFFD8 == -40 as int16) as math.nan in the temperature list
    for t in sample.temperatures:
        assert math.isnan(t), "expected nan-encoded missing temps for this fixture"

    assert bms._voltages == exp["cell_voltages_mv"]


def _feed(bms, chunks):
    got = []
    bms._fetch_futures.set_result = lambda func, buf: got.append((func, bytes(buf)))
    for c in chunks:
        bms._notification_handler(None, bytes(c))
    return got


def _chunks(raw, cut_points):
    out, prev = [], 0
    for c in list(cut_points) + [len(raw)]:
        out.append(raw[prev:c])
        prev = c
    return out


def test_ant_reassembly_fragment_ending_in_0x55():
    # #431: a fragment that happens to end in 0x55 mid-frame used to make the
    # handler judge the frame complete, find it short, and drop it.
    from bmslib.models.ant import calc_crc16
    raw = bytearray(ant_fixtures.INLINE_8S["raw"])
    raw[34:36] = (3413).to_bytes(2, "little")  # cell 1 = 0x0D55 -> byte 0x55 at [34]
    n = raw[5]
    raw[6 + n:8 + n] = bytes(calc_crc16(raw[1:6 + n]))
    raw = bytes(raw)
    bms = AntBt("00:11:22:33:44:55", name="ant")
    got = _feed(bms, _chunks(raw, [20, 35]))  # second fragment ends in 0x55
    assert got == [(0x11, raw)]


def test_ant_reassembly_20_byte_mtu():
    raw = bytes(ant_fixtures.INLINE_8S["raw"])
    bms = AntBt("00:11:22:33:44:55", name="ant")
    got = _feed(bms, _chunks(raw, range(20, len(raw), 20)))
    assert got == [(0x11, raw)]


def test_ant_reassembly_extra_section():
    # "Case C" long frame: header + payload + crc + extra(4+h) + crc + AA 55.
    # Captured shape from the spec (#431): 0x12 reply carrying FF 01 00 04 + 4 bytes.
    from bmslib.models.ant import calc_crc16
    payload = bytes(range(16))
    head = bytes([0x7e, 0xa1, 0x12, 0x40, 0x02, len(payload)]) + payload
    frame = head + bytes(calc_crc16(head[1:]))
    extra = bytes([0xff, 0x01, 0x00, 0x04, 0x4e, 0x12, 0x09, 0x00])
    raw = frame + extra + bytes(calc_crc16(extra)) + b"\xaa\x55"
    bms = AntBt("00:11:22:33:44:55", name="ant")
    got = _feed(bms, _chunks(raw, [20]))
    assert len(got) == 1 and got[0][0] == 0x12 and got[0][1].startswith(frame)
    # the following reply still gets through
    raw2 = bytes(ant_fixtures.INLINE_8S["raw"])
    got = _feed(bms, _chunks(raw2, range(20, len(raw2), 20)))
    assert got == [(0x11, raw2)]


def test_ant_negative_temperatures():
    # temperatures are int16: -5 °C must not decode as 65531
    raw = bytearray(ant_fixtures.INLINE_8S["raw"])
    t0 = 34 + 8 * 2  # first NTC after 8 cells
    raw[t0:t0 + 2] = (-5).to_bytes(2, "little", signed=True)
    raw[t0 + 4:t0 + 6] = (-3).to_bytes(2, "little", signed=True)  # mos
    bms = AntBt("00:11:22:33:44:55", name="ant")
    sample = run_fetch_with_response(bms, raw)
    assert sample.temperatures[0] == -5
    assert math.isnan(sample.temperatures[1])
    assert sample.mos_temperature == -3
