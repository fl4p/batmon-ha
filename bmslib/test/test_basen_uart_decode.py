"""Basen wired (RS232/RS485) decode tests.

The INFO fixture is the real example response frame published in the
GHswitt/esphome-basen README, and the request/checksum fixtures are the
worked examples from its checksum_test.py.
"""
import pytest

from bmslib.models.basen_uart import (
    build_frame, parse_frame, decode_info, decode_status_bitmask, _checksum,
    CMD_INFO,
)


def _h(s: str) -> bytes:
    return bytes.fromhex("".join(s.split()))


# README "Example Response Frame" for COMMAND_INFO (address 0x01).
INFO_FRAME = _h("""
7e 01 01 7c 01 10 0d 44 0d 41 0d 40 0d 41 0d 40
0d 3f 0d 40 0d 41 0d 43 0d 42 0d 42 0d 42 0d 43
0d 42 0d 42 0d 45 02 01 75 30 03 01 27 0f 04 01
6d 60 05 06 00 47 00 48 00 48 00 47 40 48 20 4b
06 05 00 00 00 00 00 00 00 00 00 00 07 01 00 1c
08 01 15 35 09 01 27 10 0a 01 00 00 0b 01 00 00
1a 05 0c 01 00 00 19 5d 0d 01 00 2b 27 ef 0e 01
00 40 00 00 0f 01 00 05 75 06 10 01 00 04 4b a2
62 0d
""")


def test_checksum_matches_reference():
    # README example command: 7e 01 01 00 fe 0d  (checksum 0xFE)
    assert _checksum(_h("7e 01 01 00")) == 0xFE
    assert build_frame(0x01, CMD_INFO) == _h("7e 01 01 00 fe 0d")


def test_parse_real_info_frame():
    f = parse_frame(INFO_FRAME)
    assert f['addr'] == 0x01 and f['cmd'] == 0x01
    assert len(f['data']) == 0x7C


def test_parse_rejects_corruption():
    with pytest.raises(ValueError):
        parse_frame(INFO_FRAME[:-2] + b"\x00" + INFO_FRAME[-1:])  # bad checksum
    with pytest.raises(ValueError):
        parse_frame(INFO_FRAME[:5])                                # truncated
    with pytest.raises(ValueError):
        parse_frame(b"\x00" + INFO_FRAME[1:])                      # bad SOI


def test_decode_info_real_frame():
    info = decode_info(parse_frame(INFO_FRAME)['data'])
    assert len(info['cell_mv']) == 16
    assert info['cell_mv'][0] == 0x0D44 == 3396
    assert info['cell_mv'][-1] == 0x0D45 == 3397
    assert info['balancing'] == 0                       # no balancing bits set
    assert info['current_a'] == pytest.approx(0.0)      # 0x7530 -> 300 - 300
    assert info['soc'] == pytest.approx(99.99)          # 0x270F
    assert info['capacity_ah'] == pytest.approx(280.0)  # 0x6D60 = 28000 /100
    assert info['temps_c'] == [21.0, 22.0, 22.0, 21.0]  # 4 normal probes
    assert info['mos_temp'] == 22.0                     # sub-type 0x40
    assert info['ambient_temp'] == 25.0                 # sub-type 0x20
    assert info['cycles'] == 28                          # 0x001C
    assert info['voltage'] == pytest.approx(54.29)      # 0x1535
    assert info['soh'] == pytest.approx(100.0)          # 0x2710


def test_decode_info_requires_status_block():
    # A valid INFO frame always carries the 0x06 status block. Dropping it must
    # NOT decode to "no problem, MOSFETs on" — it must raise.
    data = bytearray(parse_frame(INFO_FRAME)['data'])
    assert data[60] == 0x06                 # the status block type byte
    data[60] = 0x00                         # unhandled type, same size -> alignment ok
    # fix the frame checksum so parse_frame accepts it and decode_info is reached
    frame = bytearray(INFO_FRAME)
    frame[4 + 60] = 0x00
    from bmslib.models.basen_uart import _checksum
    frame[-2] = _checksum(frame[:-2])
    with pytest.raises(ValueError, match="missing status block"):
        decode_info(parse_frame(bytes(frame))['data'])


def test_status_bitmask_all_clear():
    info = decode_info(parse_frame(INFO_FRAME)['data'])
    st = decode_status_bitmask(info['status_bitmask'])
    assert st['problem'] is False and st['problem_code'] == 0
    assert st['charge_mosfet'] is True and st['discharge_mosfet'] is True
    assert st['charging'] is False and st['discharging'] is False


def test_status_bitmask_benign_bits_dont_alarm():
    # Byte 3 bit0=Charging, bit1=Discharging, byte5 = manual-MOS/heating: all
    # benign (kind 0). They must not raise `problem`.
    bm = bytearray(10)
    bm[3] = 0x01           # Charging
    bm[5] = 0x1F           # manual MOS open/off + heating pad
    st = decode_status_bitmask(bytes(bm))
    assert st['charging'] is True
    assert st['problem'] is False and st['problem_code'] == 0


def test_status_bitmask_real_fault_alarms():
    # Byte 3 bit4 (0x10) = Overvoltage Protection (kind 2, fault).
    bm = bytearray(10)
    bm[3] = 0x10
    st = decode_status_bitmask(bytes(bm))
    assert st['problem'] is True and st['problem_code'] != 0


def test_current_direction_from_status_bits():
    import asyncio

    from bmslib.models.basen_uart import BasenUart

    def make(bm3):
        data = bytearray(parse_frame(INFO_FRAME)['data'])
        # Force a non-zero current magnitude: 0x02 block value 0x6978 -> |300-270| = 30 A
        # locate the 0x02 block (right after the 33-byte cell block + version)
        # version(1)+cellblock(1+32)=34 -> block starts at data[34]=0x02
        assert data[34] == 0x02
        data[36], data[37] = 0x69, 0x78   # 0x6978 = 27000 -> 300-270 = 30
        # set the 0x06 status bitmask byte 3
        # find 0x06 block: after 02(4)03(4)04(4)05(14) from 34 -> 34+4+4+4+14=60
        assert data[60] == 0x06
        data[62 + 3] = bm3                # status byte offset 3
        return bytes(data)

    async def run(bm3):
        bms = BasenUart.__new__(BasenUart)
        bms._last_cells = []
        bms._last_temps = []
        payload = make(bm3)

        async def _read(cmd):
            return {'data': payload}
        bms._read = _read
        return await bms.fetch()

    charging = asyncio.run(run(0x01))    # Charging bit
    discharging = asyncio.run(run(0x02))  # Discharging bit
    idle = asyncio.run(run(0x00))         # neither bit -> ambiguous branch
    assert charging.current == pytest.approx(-30.0)    # charge -> negative
    assert discharging.current == pytest.approx(30.0)  # discharge -> positive
    # GHswitt raw is +charging (+30); with no direction bit set batmon still
    # negates it, so a small residual charging current reads negative, not +30.
    assert idle.current == pytest.approx(-30.0)


def test_bmssample_survives_known_soc_unknown_charge():
    # basen_uart passes charge=capacity_ah (may be nan) with a finite soc; the
    # shared BmsSample must not crash on round(nan) deriving capacity.
    from bmslib.bms import BmsSample
    s = BmsSample(voltage=52.0, current=0.0, charge=float('nan'), soc=95.0)
    assert s.soc == 95.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
