"""DalyBt over BLE against the #416 module: the empty wake-up write is refused, and
the module was only ever seen answering host address 0x40."""

import asyncio

from bmslib.models.daly import DalyBt

FFF1 = '0000fff1-0000-1000-8000-00805f9b34fb'
FFF2 = '0000fff2-0000-1000-8000-00805f9b34fb'

# replies from the #416 snoop log
REPLIES = {
    0x90: bytes.fromhex('a501900800850000753001e44d'),
    0x94: bytes.fromhex('a501940804020000000000438b'),
}


class GattError(Exception):
    pass


class Module416:
    """fff1 notify / fff2 write; rejects empty writes like the real module did
    (GATT error 258), and answers requests whose host-address byte is in `answers`."""

    def __init__(self, answers=(0x40,)):
        self.answers = answers
        self.is_connected = False
        self.services = []
        self._cb = None
        self.sent = []

    async def connect(self, timeout=20):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, char, cb, **kw):
        if char != FFF1:
            raise GattError('Characteristic %s was not found!' % char)
        self._cb = cb
        cb(None, bytearray(b'char1_ntf_data'))  # the template value it notifies on subscribe

    async def stop_notify(self, char):
        self._cb = None

    async def write_gatt_char(self, char, data, response=None):
        if char != FFF2:
            raise GattError('Characteristic %s was not found!' % char)
        if not data:
            raise GattError('Bluetooth GATT Error handle=20 error=258 description=Unknown error')
        data = bytes(data)
        self.sent.append(data)
        if data[1] in self.answers and data[2] in REPLIES:
            self._cb(None, bytearray(REPLIES[data[2]]))


def _bms(device):
    bms = DalyBt('test_jbd', name='daly416')  # test_ address: no real BLE client
    bms.client = device
    bms.TIMEOUT = 0.2
    return bms


def test_refused_empty_write_keeps_the_fff1_layout():
    bms = _bms(Module416())
    asyncio.run(bms.connect())
    assert (bms.UUID_RX, bms.UUID_TX) == (FFF1, FFF2)


def test_falls_back_to_host_address_0x40_and_pins_it():
    dev = Module416(answers=(0x40,))
    bms = _bms(dev)

    async def run():
        await bms.connect()
        try:
            await bms._q(0x90)
            raise AssertionError('0x80 must not be answered by this module')
        except TimeoutError:
            pass
        resp = await bms._q(0x90)  # now with 0x40
        assert resp == REPLIES[0x90][4:-1]
        await bms._q(0x94)
        return [f[1] for f in dev.sent]

    assert asyncio.run(run()) == [0x80, 0x40, 0x40]
    assert bms._addr_confirmed


def test_module_answering_0x80_never_switches():
    dev = Module416(answers=(0x80,))
    bms = _bms(dev)

    async def run():
        await bms.connect()
        await bms._q(0x90)
        await bms._q(0x94)

    asyncio.run(run())
    assert [f[1] for f in dev.sent] == [0x80, 0x80]
    assert bms._ble_addr_byte is None


def test_no_switch_after_a_valid_reply():
    dev = Module416(answers=(0x80,))
    bms = _bms(dev)

    async def run():
        await bms.connect()
        await bms._q(0x90)
        dev.answers = ()  # goes silent later: that's a link problem, not the address
        try:
            await bms._q(0x90)
        except TimeoutError:
            pass

    asyncio.run(run())
    assert bms._ble_addr_byte is None
