"""DalyBt.connect() over BLE: the layout and host address are taken only when a
request is answered (#416: empty write refused, module only seen answering 0x40)."""

import asyncio

from bmslib.models.daly import DalyBt

FFF1 = '0000fff1-0000-1000-8000-00805f9b34fb'
FFF2 = '0000fff2-0000-1000-8000-00805f9b34fb'
FF01 = '0000ff01-0000-1000-8000-00805f9b34fb'
FF02 = '0000ff02-0000-1000-8000-00805f9b34fb'

# replies from the #416 snoop log
REPLIES = {
    0x90: bytes.fromhex('a501900800850000753001e44d'),
    0x94: bytes.fromhex('a501940804020000000000438b'),
}


class GattError(Exception):
    pass


class Module:
    """A Daly BLE module. Default: the #416 one -- fff1 notify / fff2 write, refuses
    empty writes (GATT error 258), answers host address 0x40 only."""

    def __init__(self, answers=(0x40,), rx=FFF1, tx=FFF2, empty_write_ok=False, delay=0.0,
                 broken_tx=(), echo=False):
        self.answers, self.rx, self.tx = answers, rx, tx
        self.empty_write_ok, self.delay, self.broken_tx, self.echo = empty_write_ok, delay, broken_tx, echo
        self.is_connected = False
        self.services = []
        self._cb = {}
        self.sent = []  # (char, frame)

    async def connect(self, timeout=20):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self._cb = {}

    async def start_notify(self, char, cb, **kw):
        if char not in (FFF1, FF01) or (char != self.rx and char == FF01):
            raise GattError('Characteristic %s was not found!' % char)
        self._cb[char] = cb
        cb(None, bytearray(b'char1_ntf_data'))  # template value notified on subscribe

    async def stop_notify(self, char):
        self._cb.pop(char, None)

    def _notify(self, data):
        cb = self._cb.get(self.rx)
        if cb:
            cb(None, bytearray(data))

    async def write_gatt_char(self, char, data, response=None):
        if char not in (FFF2, FF02) or char in self.broken_tx:
            raise GattError('Characteristic %s was not found!' % char)
        if not data:
            if not self.empty_write_ok:
                raise GattError('Bluetooth GATT Error handle=20 error=258 description=Unknown error')
            return
        data = bytes(data)
        self.sent.append((char, data))
        if char != self.tx:
            return
        if self.echo:
            reply = data
        elif data[1] in self.answers and data[2] in REPLIES:
            reply = REPLIES[data[2]]
        else:
            return
        if self.delay:
            asyncio.get_running_loop().call_later(self.delay, self._notify, reply)
        else:
            self._notify(reply)


def _bms(device):
    bms = DalyBt('test_jbd', name='daly')  # test_ address: no real BLE client
    bms.client = device
    bms.TIMEOUT = 0.2
    bms.HANDSHAKE_TIMEOUT = 0.2
    return bms


def _addr_bytes(dev):
    return [f[1] for _, f in dev.sent]


def test_416_module_refusing_the_empty_write_is_found_on_0x40():
    dev = Module()
    bms = _bms(dev)

    async def run():
        await bms.connect()
        return await bms._q(0x90)

    assert asyncio.run(run()) == REPLIES[0x90][4:-1]
    assert (bms.UUID_RX, bms.UUID_TX, bms._ble_addr_byte) == (FFF1, FFF2, 0x40)
    assert [(f[1], f[2]) for _, f in dev.sent] == [(0x80, 0x90), (0x40, 0x94), (0x40, 0x90)]


def test_0x80_module_costs_one_request_and_keeps_0x80():
    dev = Module(answers=(0x80,), empty_write_ok=True)
    bms = _bms(dev)
    asyncio.run(bms.connect())
    assert bms._ble_addr_byte == 0x80 and _addr_bytes(dev) == [0x80]


def test_late_0x80_reply_does_not_confirm_0x40():
    # the review's failure: a slow 0x80 reply arriving while 0x40 is being tried
    dev = Module(answers=(0x80,), empty_write_ok=True, delay=0.3)
    bms = _bms(dev)

    async def run():
        await bms.connect()
        await asyncio.sleep(0.5)  # let the late replies land
        dev.delay = 0
        return await bms._q(0x90)

    asyncio.run(run())
    assert bms._ble_addr_byte in (None, 0x80)
    assert _addr_bytes(dev)[-1] == 0x80  # sampling still talks 0x80


def test_echoed_request_confirms_nothing():
    dev = Module(empty_write_ok=True, echo=True)
    bms = _bms(dev)
    asyncio.run(bms.connect())
    assert bms._ble_addr_byte is None  # fell back to the old behaviour, 0x80
    assert bms.UUID_TX == FFF2


def test_unusable_fff2_falls_through_to_ff01_ff02():
    # fff1 subscribes but fff2 is unusable; the module works on ff01/ff02
    dev = Module(answers=(0x80,), rx=FF01, tx=FF02, empty_write_ok=True, broken_tx=(FFF2,))
    bms = _bms(dev)
    asyncio.run(bms.connect())
    assert (bms.UUID_RX, bms.UUID_TX, bms._ble_addr_byte) == (FF01, FF02, 0x80)


def test_reconnect_tries_the_learned_address_first():
    dev = Module()
    bms = _bms(dev)

    async def run():
        await bms.connect()
        await bms.disconnect()
        dev.sent.clear()
        await bms.connect()

    asyncio.run(run())
    assert [(f[1], f[2]) for _, f in dev.sent] == [(0x40, 0x90)]


def test_silent_module_with_refused_empty_write_is_an_error(monkeypatch):
    import pytest
    import bmslib.models.daly as daly_mod

    async def no_enum(*a, **kw):
        pass

    monkeypatch.setattr(daly_mod, 'enumerate_services', no_enum)
    bms = _bms(Module(answers=()))
    with pytest.raises(Exception, match='not found'):
        asyncio.run(bms.connect())
