"""PACE paceic (wired) decode tests.

Fixtures are the real v25 example frames from nkinnan/esphome-pace-bms:
  exampleReadAnalogInformationResponseV25
  exampleReadStatusInformationResponseV25
"""
import pytest

from bmslib.models.pace import (
    build_frame, parse_frame, decode_analog, decode_status,
    CID2_READ_ANALOG, CID2_READ_STATUS,
)

ANALOG_REQ = b"~25014642E00201FD30\r"
ANALOG_RESP = (b"~25014600F07A0001100CC70CC80CC70CC70CC70CC50CC60CC70CC70CC60CC70CC6"
               b"0CC60CC70CC60CC7060B9B0B990B990B990BB30BBCFF1FCCCD12D303286A008C2710E1E4\r")
STATUS_REQ = b"~25014644E00201FD2E\r"
STATUS_RESP = (b"~25014600004C0001100000000000000000000000000000000006000000000000"
               b"00000000000E000000000000EF3A\r")


def test_build_frame_matches_reference():
    assert build_frame(0x25, 0x01, 0x46, CID2_READ_ANALOG, b"01") == ANALOG_REQ
    assert build_frame(0x25, 0x01, 0x46, CID2_READ_STATUS, b"01") == STATUS_REQ


def test_parse_rejects_corruption():
    with pytest.raises(ValueError):
        parse_frame(ANALOG_RESP[:-2] + b"0" + ANALOG_RESP[-1:])  # bad checksum
    with pytest.raises(ValueError):
        parse_frame(ANALOG_RESP[:10])                             # truncated
    with pytest.raises(ValueError):
        parse_frame(b"25014642E00201FD30\r")                      # no SOI


def test_decode_analog_real_frame():
    a = decode_analog(parse_frame(ANALOG_RESP)["info"])
    assert len(a["cell_mv"]) == 16
    assert a["cell_mv"][0] == 0x0CC7 == 3271
    assert a["temps_c"] == pytest.approx([24.1, 23.9, 23.9, 23.9, 26.5, 27.4])
    assert a["current_a"] == pytest.approx(-2.25)   # FF1F = -225 * 10 mA
    assert a["total_v"] == pytest.approx(52.429)    # CCCD mV
    assert a["remaining_ah"] == pytest.approx(48.19)
    assert a["full_ah"] == pytest.approx(103.46)
    assert a["design_ah"] == pytest.approx(100.0)
    assert a["cycles"] == 140


def test_decode_status_real_frame():
    s = decode_status(parse_frame(STATUS_RESP)["info"])
    # Healthy example capture: no protection/fault trips.
    assert s["problem_code"] == 0
    assert s["problem"] is False


def test_analog_info_fully_consumed():
    info = parse_frame(ANALOG_RESP)["info"]
    from bmslib.models.pace import _Cursor
    # re-run decode and confirm the cursor lands exactly at end of payload
    c = _Cursor(info)
    c.u8(); c.u8()                       # unknown + pack id
    n = c.u8(); [c.u16() for _ in range(n)]
    t = c.u8(); [c.u16() for _ in range(t)]
    c.s16(); c.u16(); c.u16(); c.u8(); c.u16(); c.u16(); c.u16()
    assert c.at_end()


def test_temperature_offset_zero_celsius():
    # 0x0AAA = 2730 tenths-K -> exactly 0.0 C
    frame = build_frame(0x25, 0x01, 0x46, 0x00,
                        b"0001" + b"01" + b"0FA0" + b"01" + b"0AAA"
                        + b"0000" + b"0FA0" + b"0000" + b"03" + b"0000" + b"0000" + b"0000")
    a = decode_analog(parse_frame(frame)["info"])
    assert a["temps_c"] == [0.0]
    assert a["cell_mv"] == [0x0FA0]


def test_serial_char_stub_is_stable_dict_key():
    # Regression for the reconnect callback-accumulation bug: a fresh stub built
    # on each connect() must collapse to one callback-dict key, not pile up.
    from bmslib.wired import SerialCharStub
    d = {}
    for _ in range(3):
        d[SerialCharStub("pace-uart", "notify")] = "handler"
    assert len(d) == 1
    # Different char/property stays distinct.
    d[SerialCharStub("pace-uart", "write")] = "h2"
    assert len(d) == 2


# --- _read response-validation gates (#2 return-code, #4 address/cid1) ---------
import asyncio
import contextlib


class _FakeFutures:
    def __init__(self, fields):
        self._fields = fields

    def acquire(self, key):
        return contextlib.nullcontext()

    async def wait_for(self, key, timeout):
        return self._fields


class _FakeClient:
    async def write_gatt_char(self, char, data):
        pass


def _bms_returning(fields):
    from bmslib.models.pace import PaceUart
    bms = PaceUart.__new__(PaceUart)
    bms.VER, bms.ADR, bms.CID1, bms._KEY, bms.TIMEOUT, bms.UUID_TX = 0x25, 0x01, 0x46, 0, 1, None
    bms.client = _FakeClient()
    bms._fetch_futures = _FakeFutures(fields)
    return bms


def test_read_rejects_device_error_code():
    bms = _bms_returning(dict(ver=0x25, adr=0x01, cid1=0x46, cid2=0x01, info=b""))
    with pytest.raises(ValueError, match="error code 0x01"):
        asyncio.run(bms._read(CID2_READ_ANALOG))


def test_read_rejects_wrong_address():
    bms = _bms_returning(dict(ver=0x25, adr=0x02, cid1=0x46, cid2=0x00, info=b""))
    with pytest.raises(ValueError, match="address mismatch"):
        asyncio.run(bms._read(CID2_READ_ANALOG))


def test_current_sign_stable_when_status_unavailable():
    # #3: with status down, the sign comes from the last resolved direction, not
    # PACE's unverified raw analog sign — so it can't silently flip between polls.
    import logging

    from bmslib.models.pace import PaceUart, decode_analog
    analog_info = parse_frame(ANALOG_RESP)["info"]
    mag = abs(decode_analog(analog_info)["current_a"])

    async def analog_only(cid2):
        if cid2 == CID2_READ_ANALOG:
            return {"info": analog_info}
        raise ValueError("status down")

    for last_charging, expect_sign in ((True, -1), (False, +1)):
        bms = PaceUart.__new__(PaceUart)
        bms.logger = logging.getLogger("t-pace")
        bms._last_charging = last_charging
        bms._last_cells = []
        bms._last_temps = []
        bms._read = analog_only
        sample = asyncio.run(bms.fetch())
        assert sample.current == pytest.approx(expect_sign * mag)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
