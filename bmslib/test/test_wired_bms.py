"""Shared wired-driver plumbing: bmslib.uart.wired and pace.split_frames."""
import asyncio

import pytest

import bmslib.uart
from bmslib.models.pace import split_frames
from bmslib.uart.wired import parse_addr_baud


class _Wrapper:
    """SerialBleakClientWrapper stand-in with a bus lock; a write hands the
    queued reply to the registered handler and records whether the lock was held."""

    def __init__(self, address, **kwargs):
        self.address = address
        self.services = []
        self.rx_thread_error = None
        self.handlers = {}
        self.reply = b''
        self.locked_on_write = []
        self.stop_raises = False
        self._lock = asyncio.Lock()

    def bus_lock(self):
        return self._lock

    async def connect(self, timeout=None):
        pass

    async def disconnect(self):
        pass

    async def start_notify(self, char, cb):
        self.handlers[char] = cb

    async def stop_notify(self, char):
        if self.stop_raises:
            raise RuntimeError('stop_notify failed')
        self.handlers.pop(char, None)

    async def write_gatt_char(self, char, data):
        self.locked_on_write.append(self._lock.locked())
        for cb in list(self.handlers.values()):
            asyncio.get_running_loop().call_soon(cb, None, self.reply)


@pytest.fixture
def wrapper(monkeypatch):
    monkeypatch.setattr(bmslib.uart, 'SerialBleakClientWrapper', _Wrapper)


def test_split_frames_drops_junk_and_keeps_partial():
    buf = bytearray(b'xx~AB\r~CD\rjunk~EF')
    assert list(split_frames(buf)) == [b'~AB\r', b'~CD\r']
    assert buf == bytearray(b'~EF')
    buf += b'\r'
    assert list(split_frames(buf)) == [b'~EF\r']
    assert buf == bytearray()


def test_split_frames_any_of_several_sois():
    buf = bytearray(b'z>A\r~B\r')
    assert list(split_frames(buf, (0x3E, 0x7E))) == [b'>A\r', b'~B\r']
    buf = bytearray(b'no start byte here\r')
    assert list(split_frames(buf)) == [] and buf == bytearray()


@pytest.mark.parametrize('spec,addr,baud', [
    (None, 7, None), ('', 7, None), ('3', 3, None), ('0x10', 16, None), ('2:19200', 2, 19200),
])
def test_parse_addr_baud(spec, addr, baud):
    assert parse_addr_baud(spec, 7) == (addr, baud)


def _drivers():
    from bmslib.models.pace import PaceUart
    from bmslib.models.basen_uart import BasenUart
    from bmslib.models.seplos_uart import SeplosUart
    from bmslib.models.braunpwr_uart import BraunPwrUart
    from bmslib.models.jbd_hv_uart import JbdHvUart
    from bmslib.models.renogy_uart import RenogyUart
    from bmslib.models.felicity_uart import FelicityUart
    return [(PaceUart, 'pace-uart'), (BasenUart, 'basen-uart'), (SeplosUart, 'seplos-uart-0'),
            (BraunPwrUart, 'braunpwr-uart-1'), (JbdHvUart, 'jbd-hv-uart-1'), (RenogyUart, 'renogy-uart-48'),
            (FelicityUart, 'felicity-uart-1')]


@pytest.mark.parametrize('cls,key', _drivers(), ids=lambda v: getattr(v, '__name__', v))
def test_connect_registers_bus_identity_and_disconnect_survives_stop_notify(wrapper, cls, key):
    bms = cls('serial', name='w', adapter='/dev/ttyUSB0')

    async def run():
        bms._buffer += b'stale'
        await bms.connect()
        assert bms._buffer == bytearray()
        assert [c.uuid_or_handle for c in bms.client.handlers] == [key]
        assert bms.UUID_RX is bms.UUID_TX
        bms.client.stop_raises = True
        await bms.disconnect()

    asyncio.run(run())


def test_pace_and_basen_hold_the_bus_lock(wrapper):
    from bmslib.models import pace, basen_uart

    async def run(bms, reply, req):
        await bms.connect()
        bms.client.reply = reply
        await req(bms)
        assert bms.client.locked_on_write == [True]

    p = pace.PaceUart('serial', name='p', adapter='/dev/ttyUSB0')
    asyncio.run(run(p, pace.build_frame(0x25, 0x01, 0x46, pace.CID2_OK),
                    lambda b: b._read(pace.CID2_READ_ANALOG)))
    b = basen_uart.BasenUart('serial', name='b', adapter='/dev/ttyUSB0')
    asyncio.run(run(b, basen_uart.build_frame(0x01, basen_uart.CMD_INFO, b'\x01'),
                    lambda b: b._read(basen_uart.CMD_INFO)))


def test_jbd_uart_q_accepts_timeout():
    # JbdBt's settings read calls self._q(cmd, timeout=...); the override must take it
    import inspect
    from bmslib.models.jbd_uart import JbdUart
    assert 'timeout' in inspect.signature(JbdUart._q).parameters
