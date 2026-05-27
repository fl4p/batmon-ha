"""No-name Modbus-over-NUS BMS decode regression test (issue #131).

The reporter pasted three captured request/response pairs from a Chinese
BMS that wraps Modbus RTU function 0x03 reads inside the Nordic UART
characteristics. We round-trip the example frames through our build/parse
helpers and decoders to lock the wire format in place.
"""

from bmslib.models.noname_modbus import (
    build_read,
    crc16_modbus,
    decode_capacity,
    decode_info,
    decode_voltages,
    parse_response,
    NoNameModbusBt,
    REG_CAP,
    REG_INFO,
    REG_VOLTS,
)


def _h(s):
    return bytes.fromhex(s.replace(" ", "").replace("\n", ""))


def _with_crc(payload_hex):
    body = _h(payload_hex)
    crc = crc16_modbus(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def test_build_read_matches_issue_examples():
    # The three request frames the reporter captured, CRC included.
    assert build_read(REG_INFO, 4) == _h("01 03 23 1C 00 04 8E 4B")
    assert build_read(REG_VOLTS, 38) == _h("01 03 D0 00 00 26 FC D0")
    assert build_read(REG_CAP, 25) == _h("01 03 D0 26 00 19 5D 0B")


def test_parse_response_validates_crc():
    # Info reply pasted from #131 — last two bytes are the device's own CRC.
    frame = _h("01 03 08 00 04 00 01 00 06 00 05 CD D5")
    payload = parse_response(frame)
    assert payload == _h("00 04 00 01 00 06 00 05")


def test_decode_info():
    payload = _h("00 04 00 01 00 06 00 05")
    cell_count, temp_count = decode_info(payload)
    assert cell_count == 4
    assert temp_count == 1


# 38 regs = 76 payload bytes: 4 live cells, 28 empty slots (0xEE49),
# then max mV, min mV, max idx, min idx, delta mV, pack centivolts.
VOLTS_PAYLOAD_HEX = (
    "0E 2D 0E 14 0E 2C 0E 2D " +           # 4 live cells
    ("EE 49 " * 28) +                       # 28 unused slots
    "0E 2D 0E 14 00 01 00 02 00 19 05 A8"   # max, min, max-i, min-i, delta, pack
)


def test_decode_voltages_with_known_cell_count():
    payload = _h(VOLTS_PAYLOAD_HEX)
    cells, pack_cv = decode_voltages(payload, cell_count=4)
    assert cells == [3629, 3604, 3628, 3629]
    assert pack_cv == 0x05A8  # 1448 → 14.48 V


def test_decode_voltages_filters_sentinels_when_count_unknown():
    payload = _h(VOLTS_PAYLOAD_HEX)
    cells, pack_cv = decode_voltages(payload, cell_count=0)
    assert cells == [3629, 3604, 3628, 3629]
    assert pack_cv == 0x05A8


def test_voltages_frame_round_trip_through_parse():
    frame = _with_crc("01 03 4C " + VOLTS_PAYLOAD_HEX)
    payload = parse_response(frame)
    cells, pack_cv = decode_voltages(payload, cell_count=4)
    assert cells == [3629, 3604, 3628, 3629]
    assert pack_cv / 100.0 == 14.48


# Capacity reply, 50 bytes (25 regs): the reporter masked the discharge-current
# cells as UU; we substitute zero and rely on _with_crc to recompute the CRC.
CAP_PAYLOAD_HEX = (
    "02 80 " +                              # reg 0:  temp 24.0 °C
    ("00 00 " * 8) +                        # regs 1-8
    "02 80 02 80 02 80 " +                  # regs 9-11: aux temps
    "00 07 " +                              # reg 12: charge 0.7 A
    "00 00 " +                              # reg 13: discharge (masked → 0)
    "00 64 00 64 " +                        # regs 14-15: SoC 100, SoH 100
    "26 F1 27 10 27 10 " +                  # regs 16-18: remaining/full/design
    "00 02 " +                              # reg 19: 2 cycles
    "00 00 00 04 00 00 00 00 00 00"         # regs 20-24
)


def test_decode_capacity():
    payload = _h(CAP_PAYLOAD_HEX)
    c = decode_capacity(payload)
    assert c["temp_c"] == 24.0           # (0x0280 - 400) / 10
    assert c["charge_a"] == 0.7          # 0x0007 / 10
    assert c["discharge_a"] == 0.0       # masked as UU in the issue
    assert c["soc"] == 100
    assert c["soh"] == 100
    assert c["remaining_ah"] == 996.9    # 0x26F1 / 10
    assert c["full_ah"] == 1000.0        # 0x2710 / 10
    assert c["design_ah"] == 1000.0
    assert c["cycles"] == 2


def test_full_fetch_round_trip():
    """Drive the public fetch() entry point with all three canned frames."""
    bms = NoNameModbusBt("00:11:22:33:44:55", name="noname")

    info_frame = _with_crc("01 03 08 00 04 00 01 00 06 00 05")
    volts_frame = _with_crc("01 03 4C " + VOLTS_PAYLOAD_HEX)
    cap_frame = _with_crc("01 03 32 " + CAP_PAYLOAD_HEX)

    # _q is called with (addr, n); we key the canned responses by addr.
    responses_by_addr = {
        REG_INFO: info_frame,
        REG_VOLTS: volts_frame,
        REG_CAP: cap_frame,
    }

    async def fake_q(addr, n):
        from bmslib.models.noname_modbus import parse_response as _pr
        return _pr(responses_by_addr[addr])

    bms._q = fake_q

    import asyncio
    sample = asyncio.run(bms.fetch())

    assert sample.voltage == 14.48
    # Issue example: 0.7 A charging, no discharge → batmon current = -0.7
    assert sample.current == -0.7
    assert sample.soc == 100
    assert sample.soh == 100
    assert sample.charge == 996.9
    assert sample.capacity == 1000.0
    assert sample.aged_capacity == 1000.0
    assert sample.num_cycles == 2
    assert list(sample.temperatures) == [24.0]

    voltages = asyncio.run(bms.fetch_voltages())
    assert voltages == [3629, 3604, 3628, 3629]


