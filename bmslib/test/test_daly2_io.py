"""Daly v2 live I/O path tests: Modbus request framing + chunked notification
reassembly + CRC validation. These cover the wire path that decode tests skip
(see test_daly2_decode.py for the payload decode)."""

import asyncio
import time

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
        self.writes = 0

    async def write_gatt_char(self, char, data, response=False):
        self.writes += 1
        self.written = bytes(data)
        self._deliver()

    def _deliver(self):
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

    with pytest.raises(asyncio.TimeoutError, match="try type: daly"):
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


def _exception_frame(func: int, code: int) -> bytes:
    body = bytes([0xD2, func | 0x80, code])
    crc = _modbus_crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def test_set_switch_writes_mosfet_register():
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms._switches = dict(charge=False, discharge=False)

    # echo back the write request the BMS would mirror (func 0x06, 8 bytes)
    req = _write_request(SWITCH_REGISTERS["discharge"], 1)
    bms.client = _FakeClient(bms, req)

    assert asyncio.run(bms.set_switch("discharge", True)) is not False
    assert bms.client.written == req
    assert bms._switches["discharge"] is True


def test_set_switch_no_echo_does_not_raise():
    # wrong register / rejected write -> BMS stays silent; must not propagate a
    # fatal timeout up through the mqtt action queue, and must use the short
    # SET_SWITCH_TIMEOUT rather than the sampling TIMEOUT
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.SET_SWITCH_TIMEOUT = 0.1
    bms.client = _FakeClient(bms, b"")  # no response delivered

    t0 = time.monotonic()
    assert asyncio.run(bms.set_switch("charge", False)) is False
    assert time.monotonic() - t0 < 1.0


def test_set_switch_rejected_with_exception_does_not_raise():
    # a BMS that answers a write with an exception frame (0x86) must be a
    # warning, not an IOError up the mqtt action queue
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.SET_SWITCH_TIMEOUT = 0.1
    bms.client = _FakeClient(bms, _exception_frame(0x06, 0x02))

    assert asyncio.run(bms.set_switch("charge", True)) is False


def test_set_switch_first_request_after_connect_retries():
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.SET_SWITCH_TIMEOUT = 0.05
    req = _write_request(SWITCH_REGISTERS["discharge"], 1)
    bms.client = _DropFirstWriteClient(bms, req)
    bms._switches = dict(charge=False, discharge=False)
    bms._first_reply_pending = True

    assert asyncio.run(bms.set_switch("discharge", True)) is not False

    assert bms.client.writes == 2
    assert bms._switches["discharge"] is True


def test_set_switch_unknown_raises():
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.client = _FakeClient(bms, b"")
    with pytest.raises(ValueError):
        asyncio.run(bms.set_switch("bogus", True))


class _DropFirstWriteClient(_FakeClient):
    """Ignores the first write, answers the second: the module that drops the
    very first request after connecting (daly.py's handshake exists for it too)."""

    async def write_gatt_char(self, char, data, response=False):
        if self.writes:
            await super().write_gatt_char(char, data, response=response)
        else:
            self.writes += 1


def test_first_request_after_connect_is_retried_once():
    fx = daly2_fixtures.AIOBMSBLE_4S
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.TIMEOUT = 0.05
    bms.client = _DropFirstWriteClient(bms, fx["raw"])
    bms._first_reply_pending = True  # what connect() sets after start_notify

    sample = asyncio.run(bms.fetch())

    assert bms.client.writes == 2
    assert sample.voltage == pytest.approx(fx["expected"]["voltage"], abs=0.05)
    assert bms._first_reply_pending is False


def test_retry_is_not_used_after_the_first_reply():
    fx = daly2_fixtures.AIOBMSBLE_4S
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.TIMEOUT = 0.05
    bms.client = _DropFirstWriteClient(bms, fx["raw"])

    with pytest.raises(asyncio.TimeoutError, match="try type: daly"):
        asyncio.run(bms.fetch())
    assert bms.client.writes == 1  # no first-reply flag -> no retry


def test_failed_first_round_is_not_retried_every_cycle():
    # a module that never answers Modbus (the `try type: daly` case) must not
    # pay two timeouts per cycle forever
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.TIMEOUT = 0.02
    bms.client = _FakeClient(bms, b"")
    bms._first_reply_pending = True

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.fetch())
    assert bms.client.writes == 2
    assert bms._first_reply_pending is False

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.fetch())
    assert bms.client.writes == 3  # one write this cycle, not two


class _TailThenReplyClient(_FakeClient):
    """Drops the first request, but the tail of its lost reply arrives just
    after the retry cleared the buffer."""

    def __init__(self, bms, response, tail):
        super().__init__(bms, response)
        self._tail = tail

    async def write_gatt_char(self, char, data, response=False):
        self.writes += 1
        if self.writes == 1:
            return
        # without the address resync, this tail is parsed as a frame head with
        # a length that swallows the retry's own reply
        self._bms._notification_handler(None, self._tail)
        self._deliver()


@pytest.mark.parametrize("tail", [
    bytes([0x00, 0x03, 0x7C]),        # does not start with the slave address
    bytes([0xD2, 0x03, 0x7C, 0x00]),  # starts with it, but is a bogus frame
])
def test_partial_frame_from_the_lost_request_does_not_swallow_the_retry(tail):
    fx = daly2_fixtures.AIOBMSBLE_4S
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.TIMEOUT = 0.05
    # a tail from the lost reply whose next bytes look like a 0x03 reply header
    # expecting a full 124-byte payload
    bms.client = _TailThenReplyClient(bms, fx["raw"], tail)
    bms._first_reply_pending = True

    sample = asyncio.run(bms.fetch())

    assert bms.client.writes == 2
    assert sample.voltage == pytest.approx(fx["expected"]["voltage"], abs=0.05)


def test_fetch_voltages_fallback_without_a_prior_fetch():
    fx = daly2_fixtures.AIOBMSBLE_4S
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.client = _FakeClient(bms, fx["raw"])

    voltages = asyncio.run(bms.fetch_voltages())

    assert bms.client.written == _read_request(0x0000, 0x003E)
    assert voltages == fx["expected"]["cell_voltages"]


def test_modbus_exception_reply_is_surfaced_not_timed_out():
    # an exception reply answers func|0x80; the waiter on the plain function
    # must see it (regression: set_result used the 0x83 key, so it timed out)
    frame = _exception_frame(0x03, 0x02)  # illegal data address

    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    bms.UUID_TX = "tx"
    bms.TIMEOUT = 0.05
    bms.client = _FakeClient(bms, frame)
    bms._first_reply_pending = True  # an exception is a reply: no retry

    with pytest.raises(IOError, match="Modbus exception 0x02"):
        asyncio.run(bms.fetch())
    assert bms.client.writes == 1
    assert bms._first_reply_pending is False
