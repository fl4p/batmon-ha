"""JKBt.fetch: when the 0x02 status stream stops on a live link, re-send 0x96 once (#391).

A JK-B2A20S20P sw 19.31 stops notifying 0x02 frames ~18,050 s into every connection while
the link stays up; a fresh 0x96 query restarts the stream.
"""

import asyncio

import pytest

from bmslib.models.dummy import JKDummy
from bmslib.models.jikong import JKBt


class _QuietJK(JKDummy):
    """No background stream. Answers 0x96 with the settings frame and, if `rearm`,
    one 0x02 status frame (the stream resuming)."""

    def __init__(self, rearm: bool):
        super().__init__(is_new_11x=True, stream=False)
        self.rearm = rearm
        self.queries_96 = 0
        self.is_connected = True

    async def write_gatt_char(self, char_specifier, data, response=False):
        await super().write_gatt_char(char_specifier, data, response)
        if bytes(data).startswith(b'\xaaU\x90\xeb\x96'):
            self.queries_96 += 1
            if self.rearm:
                self._callbacks[JKBt.CHAR_UUID](self, bytes(self.MSGS[1]))


def _make(rearm: bool):
    bms = JKBt("00:11:22:33:44:55", name="jk")
    bms.TIMEOUT = 0.3
    client = _QuietJK(rearm)
    bms.client = client
    bms.char_handle_write = JKBt.CHAR_UUID
    bms.is_new_11fw_32s = True
    bms._has_float_charger = False
    asyncio.run(client.start_notify(JKBt.CHAR_UUID, bms._notification_handler))
    return bms, client


def test_stalled_stream_is_rearmed_by_0x96():
    bms, client = _make(rearm=True)
    sample = asyncio.run(bms.fetch())
    assert client.queries_96 >= 1
    assert sample.voltage > 0


def test_rearm_that_gets_no_answer_still_times_out():
    # the sampler's no-data watchdog must still see the timeout and reconnect
    bms, client = _make(rearm=False)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.fetch())
    assert client.queries_96 == 1


def test_no_rearm_when_disconnected():
    bms, client = _make(rearm=True)
    client.is_connected = False
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bms.fetch())
    assert client.queries_96 == 0
