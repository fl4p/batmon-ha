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
    # the extra section + AA 55 arrive in their own notification, after the
    # frame was already accepted at its CRC
    got = _feed(bms, _chunks(raw, [20, len(frame)]))
    assert len(got) == 1 and got[0][0] == 0x12 and got[0][1] == frame
    # the orphaned tail is dropped, not kept as the start of a bogus frame
    assert not bms._buffer
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


def test_ant_reassembly_lost_header_fragment():
    # the fragment carrying 7E A1 is lost: the headerless rest must not be
    # buffered, so the next reply decodes cleanly
    raw = bytes(ant_fixtures.INLINE_8S["raw"])
    chunks = _chunks(raw, range(20, len(raw), 20))
    bms = AntBt("00:11:22:33:44:55", name="ant")
    got = _feed(bms, chunks[1:] + chunks)
    assert got == [(0x11, raw)]


def _with_crc(raw):
    from bmslib.models.ant import calc_crc16
    n = raw[5]
    raw[6 + n:8 + n] = bytes(calc_crc16(raw[1:6 + n]))
    return raw


def test_ant24_fields_past_the_basics():
    s = run_fetch_with_response(AntBt("00:11:22:33:44:55", name="ant"), ant_fixtures.ANT24_8S["raw"])
    assert s.uptime == 59505998  # s; the next frame, 30.2 s later, read 59506029
    assert s.extra_values['ant/balancer_temperature'] == 23
    assert s.extra_values['ant/total_discharged'] == 27357.425
    assert s.extra_values['ant/total_charged'] == 24553.663
    assert math.isnan(s.runtime)  # this firmware's frame ends before the remaining-time field
    assert s.problem is False and s.problem_code == 0
    assert s.extra_values['ant/protection'] == 'none'
    assert s.extra_values['ant/flags'] == ('charging, charger connected, charge MOSFET on, discharge MOSFET on, '
                                           'balance spread')
    for k in s.extra_values:
        assert k in s.extra_desc


def test_ant_cell_voltage_masks_balancer_error_bits():
    raw = bytearray(ant_fixtures.ANT24_8S["raw"])
    raw[35] |= 0xA0  # cell 1 = 0x0D88 | 0xA000: balancer error state 5
    s_raw = _with_crc(raw)
    bms = AntBt("00:11:22:33:44:55", name="ant")
    run_fetch_with_response(bms, s_raw)
    assert bms._voltages[0] == 3464


def test_ant_protection_flags():
    raw = bytearray(ant_fixtures.ANT24_8S["raw"])
    raw[10 + 2] = 0x06  # protect bits 17, 18: discharge held off by command
    s = run_fetch_with_response(AntBt("00:11:22:33:44:55", name="ant"), _with_crc(bytearray(raw)))
    assert s.problem is False and s.problem_code == 0
    assert s.extra_values['ant/protection'] == 'discharge switch control 1, discharge switch control 2'

    raw[10] = 0x02  # bit 1: cell overvoltage
    s = run_fetch_with_response(AntBt("00:11:22:33:44:55", name="ant"), _with_crc(bytearray(raw)))
    assert s.problem is True and s.problem_code == 2
    assert s.extra_values['ant/protection'].startswith('cell overvoltage, discharge switch control 1')


def test_ant_short_frame_leaves_the_trailing_fields_out():
    full = ant_fixtures.ANT24_8S["raw"]
    n = full[5] - 32  # cut before the charged/discharged totals
    raw = bytearray(full[:6 + n]) + b"\0\0\xaa\x55"
    raw[5] = n
    s = run_fetch_with_response(AntBt("00:11:22:33:44:55", name="ant"), _with_crc(raw))
    assert 'ant/total_charged' not in s.extra_values and math.isnan(s.runtime)
    assert s.uptime == 59505998 and s.voltage == pytest.approx(27.36)


def test_ant_frame_too_short_for_the_mandatory_fields_raises():
    full = ant_fixtures.ANT24_8S["raw"]
    n = 34 - 6 + 2 * 10 + 20  # ends inside the capacity fields
    raw = bytearray(full[:6 + n]) + b"\0\0\xaa\x55"
    raw[5] = n
    with pytest.raises(ValueError, match="too short"):
        run_fetch_with_response(AntBt("00:11:22:33:44:55", name="ant"), _with_crc(raw))


def test_ant_frame_ending_after_the_cycled_capacity_decodes():
    full = ant_fixtures.ANT24_8S["raw"]
    n = 28 + 2 * 10 + 28  # header fields, 8 cells + 2 probes, up to the cycled capacity; no power field
    raw = bytearray(full[:6 + n]) + b"\0\0\xaa\x55"
    raw[5] = n
    s = run_fetch_with_response(AntBt("00:11:22:33:44:55", name="ant"), _with_crc(raw))
    assert s.total_charge_throughput == pytest.approx(25955.544) and math.isnan(s.uptime)
