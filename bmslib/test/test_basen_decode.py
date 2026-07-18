"""Basen BLE decode tests.

Fixtures are the captured example frames from syssi/esphome-basen-bms
(docs/protocol-design.md).
"""
import pytest

from bmslib.models.basen import (
    build_frame, parse_frame, decode_status, decode_general_info,
    decode_cell_voltages, FT_STATUS, FT_GENERAL_INFO, FT_CELL_VOLTAGES_1_12,
)


def _h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


STATUS_RESP = _h("3B16 2A18 00000000 CE610000 12141919 63230000 8080 0000 08 02 0000 6F03 0D0A")
GENERAL_RESP = _h("3A16 2B18 A0860100 00640000 91A00100 00000000 3075 0000 7153 0700 8604 0D0A")
CELLS_RESP = _h("3A16 2418 960C 970C 980C 960C 960C 980C 980C 970C 0000 0000 0000 0000 6A05 0D0A")


def test_build_request_matches_reference():
    # Documented requests: status 3b162a010041000d0a, general 3a162b010042000d0a
    assert build_frame(FT_STATUS, sof=0x3B) == _h("3B16 2A01 00 4100 0D0A")
    assert build_frame(FT_GENERAL_INFO, sof=0x3A) == _h("3A16 2B01 00 4200 0D0A")
    assert build_frame(FT_CELL_VOLTAGES_1_12, sof=0x3A) == _h("3A16 2401 00 3B00 0D0A")


def test_parse_rejects_corruption():
    with pytest.raises(ValueError):
        parse_frame(STATUS_RESP[:-3] + b"\x00" + STATUS_RESP[-2:])  # bad checksum
    with pytest.raises(ValueError):
        parse_frame(STATUS_RESP[:6])                                # truncated
    with pytest.raises(ValueError):
        parse_frame(b"\x00" + STATUS_RESP[1:])                      # bad SOF


def test_decode_status_real_frame():
    f = parse_frame(STATUS_RESP)
    assert f['frame_type'] == FT_STATUS
    s = decode_status(f['data'])
    assert s['current_charging'] == pytest.approx(0.0)
    assert s['total_v'] == pytest.approx(25.038)     # 0x000061CE mV
    assert s['temps_c'] == [18.0, 20.0, 25.0, 25.0]
    assert s['remaining_ah'] == pytest.approx(9.059)  # 0x00002363 mAh
    assert s['soc'] == 8
    assert s['charge_mosfet'] is True                 # 0x80 bit7
    assert s['discharge_mosfet'] is True
    assert s['problem'] is False


def test_decode_general_info_real_frame():
    g = decode_general_info(parse_frame(GENERAL_RESP)['data'])
    assert g['nominal_ah'] == pytest.approx(100.0)    # 0x000186A0 mAh
    assert g['nominal_v'] == pytest.approx(25.6)      # 0x00006400 mV
    assert g['real_ah'] == pytest.approx(106.641)     # 0x0001A091 mAh
    assert g['cycles'] == 7


def test_decode_cell_voltages_real_frame():
    cells = decode_cell_voltages(parse_frame(CELLS_RESP)['data'])
    assert cells[:8] == [3222, 3223, 3224, 3222, 3222, 3224, 3224, 3223]  # 0x0C96..
    assert cells[8:] == [0, 0, 0, 0]                  # unpopulated slots


def _status_with(b20: int, b22: int) -> bytes:
    """Rebuild STATUS_RESP with charging-state byte [20] and charging-warning
    byte [22] overridden, fixing up the CRC so parse_frame accepts it."""
    d = bytearray(STATUS_RESP)
    d[20] = b20
    d[22] = b22
    crc = sum(d[1:28]) & 0xFFFF          # bytes[1 .. data_len+3], data_len=0x18
    d[28], d[29] = crc & 0xFF, crc >> 8
    return bytes(d)


def test_hard_protection_trip_sets_problem():
    # Cell-overvoltage trip (state bit3), charge MOSFET opened, no warning bits.
    s = decode_status(parse_frame(_status_with(b20=0x08, b22=0x00))['data'])
    assert s['charge_mosfet'] is False
    assert s['problem'] is True
    assert s['problem_code'] & 0x08


def test_fully_charged_warning_is_not_a_problem():
    # "Fully charged" charge-warning bit (0x10) is benign; MOSFETs on.
    s = decode_status(parse_frame(_status_with(b20=0x80, b22=0x10))['data'])
    assert s['problem'] is False
    assert s['problem_code'] == 0


def test_fetch_voltages_preserves_established_dead_cell():
    import asyncio
    import logging

    from bmslib.models.basen import BasenBt, FT_CELL_VOLTAGES_1_12

    bms = BasenBt.__new__(BasenBt)          # skip BLE __init__
    bms._cell_count = 0
    bms._last_cells = []
    bms.logger = logging.getLogger("test-basen")
    healthy = decode_cell_voltages(parse_frame(CELLS_RESP)['data'])  # 8 cells + padding

    def chunk1_only(frame):
        async def _read(frame_type):
            if frame_type == FT_CELL_VOLTAGES_1_12:
                return frame
            raise ValueError("no second chunk")
        return _read

    bms._read = chunk1_only(parse_frame(CELLS_RESP)['data'])
    first = asyncio.run(bms.fetch_voltages())
    assert first == healthy[:8]             # padding trimmed, 8 real cells

    # Now cell 8 dies (reads 0 mV). It must NOT be trimmed as padding.
    dead = bytearray(CELLS_RESP)
    dead[18], dead[19] = 0x00, 0x00         # cell 8 (offset 4 + 7*2) -> 0
    crc = sum(dead[1:28]) & 0xFFFF
    dead[28], dead[29] = crc & 0xFF, crc >> 8

    bms._read = chunk1_only(bytes(dead))
    second = asyncio.run(bms.fetch_voltages())
    assert len(second) == 8                 # still 8 cells, dead cell preserved
    assert second[7] == 0
