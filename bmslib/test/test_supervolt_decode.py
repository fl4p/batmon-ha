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
