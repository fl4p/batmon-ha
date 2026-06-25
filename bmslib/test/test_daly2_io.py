"""Daly v2 live I/O path tests: Modbus request framing + chunked notification
reassembly + CRC validation. These cover the wire path that decode tests skip
(see test_daly2_decode.py for the payload decode)."""

import asyncio

import pytest

from bmslib.models.daly2 import (
    Daly2Bt, _read_request, _write_request, _modbus_crc16, SWITCH_REGISTERS,
)
from bmslib.test.data import daly2_fixtures


def test_read_request_matches_known_frame():
    # The request the official app / ESPHome send for the 124-byte info block.
    assert _read_request(0x0000, 0x003E) == bytes.fromhex("d203000000 3ed7b9".replace(" ", ""))


def test_crc16_roundtrip():
    frame = daly2_fixtures.AIOBMSBLE_4S["raw"]
    assert _modbus_crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


class _FakeClient:
    """Captures the written request and replays the fixture response in MTU-sized
    chunks through the notification handler, mimicking a real BLE link."""

    def __init__(self, bms, response, chunk=20):
        self._bms = bms
        self._response = response
        self._chunk = chunk
        self.written = None

    async def write_gatt_char(self, char, data, response=False):
        self.written = bytes(data)
        # deliver the response split across several notifications
        for i in range(0, len(self._response), self._chunk):
            self._bms._notification_handler(None, self._response[i:i + self._chunk])


def test_fetch_over_chunked_notifications():
    fx = daly2_fixtures.AIOBMSBLE_4S
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.client = _FakeClient(bms, fx["raw"])

    sample = asyncio.run(bms.fetch())

    # the command was actually sent (regression: the old _q never wrote anything)
    assert bms.client.written == _read_request(0x0000, 0x003E)
    assert sample.voltage == pytest.approx(fx["expected"]["voltage"], abs=0.05)
    assert sample.soc == pytest.approx(fx["expected"]["soc"], abs=0.05)
    assert list(sample.temperatures) == fx["expected"]["temperatures"]


def test_bad_crc_is_ignored_and_times_out():
    fx = daly2_fixtures.AIOBMSBLE_4S
    corrupt = bytearray(fx["raw"])
    corrupt[10] ^= 0xFF  # flip a payload byte -> CRC mismatch

    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.TIMEOUT = 0.2
    bms.client = _FakeClient(bms, bytes(corrupt))

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.fetch())


def test_switch_state_decoded_from_mosfet_registers():
    fx = daly2_fixtures.AIOBMSBLE_4S
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.client = _FakeClient(bms, fx["raw"])

    sample = asyncio.run(bms.fetch())
    assert dict(sample.switches) == fx["expected"]["switches"]


def test_fetch_voltages_reuses_block_and_decodes_cells():
    fx = daly2_fixtures.AIOBMSBLE_4S
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.client = _FakeClient(bms, fx["raw"])

    asyncio.run(bms.fetch())
    bms.client.written = None  # fetch_voltages must NOT issue another read
    voltages = asyncio.run(bms.fetch_voltages())

    assert bms.client.written is None
    assert voltages == fx["expected"]["cell_voltages"]


def test_switch_writes_match_official_app_snoop():
    # Exact frames captured from the official Daly app's HCI snoop (#356):
    #   charge on/off    -> D2 06 00 A5 00 01/00
    #   discharge on/off -> D2 06 00 A6 00 01/00
    assert SWITCH_REGISTERS == dict(charge=0x00A5, discharge=0x00A6)
    assert _write_request(0x00A5, 1) == bytes.fromhex("d20600a500014b8a")
    assert _write_request(0x00A5, 0) == bytes.fromhex("d20600a500008a4a")
    assert _write_request(0x00A6, 1) == bytes.fromhex("d20600a60001bb8a")
    assert _write_request(0x00A6, 0) == bytes.fromhex("d20600a600007a4a")


def test_set_switch_writes_mosfet_register():
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"

    # echo back the write request the BMS would mirror (func 0x06, 8 bytes)
    req = _write_request(SWITCH_REGISTERS["discharge"], 1)
    bms.client = _FakeClient(bms, req)

    asyncio.run(bms.set_switch("discharge", True))
    assert bms.client.written == req


def test_set_switch_no_echo_does_not_raise():
    # wrong register / rejected write -> BMS stays silent; must not propagate a
    # fatal timeout up through the mqtt action queue
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.SET_SWITCH_TIMEOUT = 0.2
    bms.client = _FakeClient(bms, b"")  # no response delivered

    asyncio.run(bms.set_switch("charge", False))  # should swallow the timeout


def test_set_switch_unknown_raises():
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.client = _FakeClient(bms, b"")
    with pytest.raises(ValueError):
        asyncio.run(bms.set_switch("bogus", True))
