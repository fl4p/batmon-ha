"""Supervolt BLE ASCII-frame decode regression test.

Supervolt sends one ASCII-hex frame per query, starting with ``:`` and ending
with ``~``. The realtime frame is exactly 128 bytes; the capacity frame is 30.
We build a synthesized realtime frame with known field values and assert the
decoded sensor dict matches.
"""

from bmslib.models.supervolt import SuperVoltBt


def _build_realtime_frame(volts_mv, ch_a_x100=0, dsg_a_x100=0, temps_c=(25, 25, 25, 25),
                          working_state=0xF000, soc=90):
    """Build a synthetic 128-byte Supervolt realtime ASCII frame."""
    frame = bytearray(128)
    frame[0] = ord(":")

    def put(off, s):
        for i, c in enumerate(s):
            frame[off + i] = ord(c)

    put(1, "10")       # address
    put(3, "02")       # command
    put(5, "50")       # version
    put(7, "0050")     # length
    put(11, "20240518123000")
    # 16 cells, each 4-char ASCII hex mV
    padded = list(volts_mv) + [0] * (16 - len(volts_mv))
    put(25, "".join(f"{v:04X}" for v in padded))
    put(89, f"{ch_a_x100:04X}")
    put(93, f"{dsg_a_x100:04X}")
    for i, t in enumerate(temps_c):
        put(97 + i * 2, f"{t + 40:02X}")
    put(105, f"{working_state:04X}")
    put(109, "00")     # alarm
    put(111, "0000")   # balance
    put(115, "0010")   # discharge count
    put(119, "0005")   # charge count
    put(123, f"{soc:02X}")
    put(125, "01")     # trailing checksum-ish byte
    frame[127] = ord("~")
    return bytes(frame)


def test_supervolt_realtime_decode_normal_charging():
    frame = _build_realtime_frame(
        volts_mv=[3300 + i for i in range(11)],   # 11s pack
        ch_a_x100=100,                            # 1.00 A charging
        dsg_a_x100=0,
        temps_c=(25, 25, 25, 25),
        working_state=0xF000,                     # "Normal"
        soc=90,
    )

    bms = SuperVoltBt("00:11:22:33:44:55", name="sv")
    bms.parseData(frame)

    # 11 cells × ~3.3 V ≈ 36.36V
    assert abs(bms.totalV - 36.355) < 0.01
    assert bms.soc == 90
    assert bms.chargingA == 1.0
    assert bms.dischargingA == 0.0
    assert bms.loadA == -1.0                       # charge → negative load
    assert bms.tempC[:4] == [25, 25, 25, 25]
    assert bms.workingState == 0xF000
    assert bms.cellV[:11] == [3300 + i for i in range(11)]
    assert bms.cellV[11:] == [None] * 5


def test_supervolt_realtime_decode_discharging():
    frame = _build_realtime_frame(
        volts_mv=[3250] * 11,
        ch_a_x100=0,
        dsg_a_x100=250,    # 2.50 A discharging
        temps_c=(30, 30, 30, 30),
        working_state=0xF002,  # Normal + discharging
        soc=42,
    )

    bms = SuperVoltBt("00:11:22:33:44:55", name="sv")
    bms.parseData(frame)

    assert bms.soc == 42
    assert bms.dischargingA == 2.5
    assert bms.loadA == 2.5
    assert bms.workingState == 0xF002


# --- golden tests: pin every field parseData sets, plus error handling ---

import logging

import pytest


def _bms(verbose=False):
    return SuperVoltBt("00:11:22:33:44:55", name="sv", verbose_log=verbose)


def _expected_total(volts_mv):
    total = 0
    for v in volts_mv[:11]:
        total += v * 1e-3
    return total


def _build_capacity_frame(remaining_x10=0, complete_x10=0, designed_x10=0):
    s = ":" + "10" + "03" + "50" + "0010" + "ABCD" \
        + f"{remaining_x10:04X}" + f"{complete_x10:04X}" + f"{designed_x10:04X}" + "01" + "~"
    assert len(s) == 30
    return s.encode()


@pytest.mark.parametrize("verbose", [False, True])
def test_realtime_golden_all_fields(verbose):
    volts = [3301, 3302, 3303, 3304, 3305, 3306, 3307, 3308, 3309, 3310, 3311, 3999, 4000, 4001, 4002, 4003]
    frame = _build_realtime_frame(volts, ch_a_x100=1234, dsg_a_x100=56, temps_c=(-5, 0, 21, 60),
                                  working_state=0xF00D, soc=77)
    bms = _bms(verbose)
    bms.parseData(bytearray(frame))

    assert bms.frame_address == 0x10 and type(bms.frame_address) is int
    assert bms.address != 0x10  # BLE address kept for reconnects
    assert bms.command == 0x02
    assert bms.version == 0x50
    assert bms.length == 0x50
    # only 11 of the 16 cells on the wire are decoded
    assert bms.cellV == volts[:11] + [None] * 5
    assert all(type(v) is int for v in bms.cellV[:11])
    assert bms.totalV == _expected_total(volts)
    assert type(bms.totalV) is float
    assert bms.chargingA == 12.34
    assert bms.dischargingA == 0.56
    assert bms.loadA == -12.34 + 0.56
    assert bms.loadA < 0
    assert bms.tempC == [-5, 0, 21, 60]
    assert bms.workingState == 0xF00D
    assert bms.alarm == 0
    assert bms.balanceState == 0
    assert bms.dischargeNumber == 0x10
    assert bms.chargeNumber == 5
    assert bms.soc == 77
    # capacity fields untouched by a realtime frame
    assert bms.remainingAh is None and bms.completeAh is None and bms.designedAh is None


def test_realtime_alarm_balance_fields():
    frame = bytearray(_build_realtime_frame([3300] * 11))
    frame[109:111] = b"A5"
    frame[111:115] = b"1F2E"
    _bms_ = _bms()
    _bms_.parseData(bytes(frame))
    assert _bms_.alarm == 0xA5
    assert _bms_.balanceState == 0x1F2E


@pytest.mark.parametrize("raw, expected", [(50000, 500.0), (50001, 0.0), (0xFFFF, 0.0)])
def test_realtime_current_over_500a_clamped(raw, expected):
    bms = _bms()
    bms.parseData(_build_realtime_frame([3300] * 11, ch_a_x100=raw, dsg_a_x100=raw))
    assert bms.chargingA == expected
    assert bms.dischargingA == expected
    assert bms.loadA == 0.0


@pytest.mark.parametrize("verbose", [False, True])
def test_capacity_golden_all_fields(verbose):
    bms = _bms(verbose)
    bms.parseData(bytearray(_build_capacity_frame(1234, 2000, 2105)))
    assert bms.frame_address == 0x10
    assert bms.command == 0x03
    assert bms.version == 0x50
    assert bms.length == 0x10
    assert bms.remainingAh == 123.4
    assert bms.completeAh == 200.0
    assert bms.designedAh == 210.5
    assert type(bms.designedAh) is float
    # realtime fields untouched by a capacity frame
    assert bms.totalV is None and bms.soc is None and bms.cellV == [None] * 16


@pytest.mark.parametrize("data", [b"", None, b":12~", b"x" * 127, b"x" * 129, b"x" * 31])
def test_wrong_length_or_empty_changes_nothing(data):
    bms = _bms()
    before = dict(vars(bms))
    bms.parseData(data)
    assert vars(bms) == before


def test_non_bytes_frames_are_ignored():
    bms = _bms()
    before = dict(vars(bms))
    bms.parseData(_build_realtime_frame([3300] * 11).decode())
    bms.parseData(_build_capacity_frame(1, 2, 3).decode())
    assert vars(bms) == before


def test_realtime_garbled_field_keeps_earlier_fields_and_logs(caplog):
    volts = [3300 + i for i in range(11)]
    frame = bytearray(_build_realtime_frame(volts, ch_a_x100=100, soc=50))
    frame[89:93] = b"ZZZZ"   # charging current
    bms = _bms()
    with caplog.at_level(logging.ERROR):
        bms.parseData(bytes(frame))
    assert bms.cellV[:11] == volts
    assert bms.totalV == _expected_total(volts)
    assert bms.chargingA is None
    assert bms.dischargingA is None
    assert bms.soc is None
    assert bms.workingState == 0
    assert "supervolt parseData failed" in caplog.text


def test_realtime_garbled_cell_leaves_partial_sum(caplog):
    volts = [3300 + i for i in range(11)]
    frame = bytearray(_build_realtime_frame(volts))
    frame[25 + 3 * 4:25 + 4 * 4] = b"\xff\xff\xff\xff"   # cell 3, non-ASCII
    bms = _bms()
    bms.parseData(bytes(frame))
    assert bms.cellV[:3] == volts[:3]
    assert bms.cellV[3:] == [None] * 13
    assert bms.totalV == _expected_total(volts[:3])
    assert "supervolt parseData failed" in caplog.text


def test_capacity_garbled_field_keeps_earlier_fields(caplog):
    frame = bytearray(_build_capacity_frame(1234, 2000, 2105))
    frame[19:23] = b"--zz"   # completeAh
    bms = _bms()
    bms.parseData(bytes(frame))
    assert bms.remainingAh == 123.4
    assert bms.completeAh is None
    assert bms.designedAh is None
    assert "supervolt parseData failed" in caplog.text


def test_reserved_and_date_fields_not_validated():
    frame = bytearray(_build_realtime_frame([3300] * 11, soc=10))
    frame[11:25] = b"not-hex-at-all"
    frame[125:127] = b"??"   # end code
    bms = _bms()
    bms.parseData(bytes(frame))
    assert bms.soc == 10
    cap = bytearray(_build_capacity_frame(1, 2, 3))
    cap[11:15] = b"????"
    cap[27:29] = b"??"
    bms.parseData(bytes(cap))
    assert bms.designedAh == 0.3


def test_notification_handler_reassembles_chunks():
    frame = _build_realtime_frame([3300] * 11, soc=66)
    bms = _bms()
    bms._notification_handler(None, bytearray(frame[:20]))
    assert not bms.notificationReceived
    bms._notification_handler(None, bytearray(frame[20:100]))
    bms._notification_handler(None, bytearray(frame[100:]))
    assert bms.notificationReceived
    assert bms.soc == 66
