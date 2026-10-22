"""
This is code for a dummy BMS wich doesn't physically exist.

"""
import math
import random
import time
from functools import partial
from threading import Thread
from typing import Callable, Union

from bmslib.bms import BmsSample
from bmslib.bt import BtBms
from bmslib.util import get_logger, dotdict


class DummyBt(BtBms):
    TEMPERATURE_STEP = .1

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._switches = dict(charge=True, discharge=True)
        self._t0 = time.time()
        self._connected = False
        self._seed = random.random() * 2 * math.pi

        self._cell_r = 5e-3
        self.I = 0

    @property
    def is_connected(self):
        return self._connected

    async def connect(self, **kwargs):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def fetch(self) -> BmsSample:
        self.I = math.sin(time.time() / 16 + self._seed)
        temp_prec = 1/self.TEMPERATURE_STEP
        sample = BmsSample(
            voltage=12 - math.sin(time.time() / 16 + self._seed) * .5,
            current=self.I,
            charge=(.5 + math.sin(time.time() / 32 + self._seed) * .5) * 100,
            capacity=100,
            num_cycles=3,
            temperatures=[round((21 + math.sin(time.time() / 256 + self._seed) + random.random()/10) * temp_prec) / temp_prec],
            mos_temperature=round((23 + math.sin(time.time() / 256 + self._seed) + random.random()/10) * temp_prec) / temp_prec,
            switches=self._switches,
            uptime=(time.time() - self._t0)
        )
        return sample

    async def fetch_voltages(self):
        #  I>0 -> dsg
        o = int(self.I * self._cell_r * -1000)
        return [3000 + o, 3010 + o, 3020 + o, 3030 + o]

    async def set_switch(self, switch: str, state: bool):
        self.logger.info('set_switch %s %s', switch, state)
        assert isinstance(state, bool)
        self._switches[switch] = state


class BleakDummyClient:
    def __init__(self, address: str, disconnected_callback):
        self.address = address
        self._connected = False
        self._disconnected_callback = disconnected_callback
        dummy_classes = dict(
            jk=JKDummy,
            jk11=partial(JKDummy, is_new_11x=True),
            jbd=JBDDummy,
        )
        self._bms = dummy_classes[address[5:]]()

    @property
    def is_connected(self):
        return self._connected

    async def connect(self, timeout=20):
        assert not self._connected
        self._connected = True

    async def disconnect(self):
        assert self._connected
        self._connected = False
        cb = self._disconnected_callback
        cb and cb(self)

    @property
    def services(self):
        return self._bms.services if hasattr(self._bms, "services") else []

    async def _connect_with_scanner(self):
        raise NotImplementedError()

    async def start_notify(self, char_specifier, callback: Callable[[int, bytearray], None]):
        return await self._bms.start_notify(char_specifier, callback)

    async def write_gatt_char(self, char_specifier, data: Union[bytes, bytearray, memoryview],
                              response: bool = False, ):
        return await self._bms.write_gatt_char(char_specifier, data, response)

    async def __aenter__(self):
        await self.connect()

    async def __aexit__(self, *args):
        await self.disconnect()

    def __await__(self):
        return self.__aexit__().__await__()


class JKDummy:
    DEVICE_INFO = b'U\xaa\xeb\x90\x03\x15JK-B2A24S20P\x00\x00\x00\x0010.X-W\x00\x0010.02\x00\x00\x00\xdc\xc6/\x00\x06\x00\x00\x00JK pw123456\x00\x00\x00\x00\x001234\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00220606\x00\x001120303218\x000000\x00Input Userdata\x00\x00123456\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xc5\xaaU\x90\xeb\xc8\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00D'

    def __init__(self, is_new_11x=False, settings_frame: Union[bytes, None] = None,
                 ignore_writes: bool = False, stream: bool = True, misroute: Union[dict, None] = None,
                 drop_writes_within_s: float = 0):
        """
        :param settings_frame: 300-byte 0x01 frame to answer with (default: the one in MSGS[0])
        :param ignore_writes: accept settings writes but don't apply them (a BMS that rejects)
        :param stream: on 0x96, also start the background thread that sends MSGS every second
        :param misroute: {register: setting name}: a write to `register` lands on that other
            setting instead (a register that means something else on this BMS)
        :param drop_writes_within_s: silently ignore a settings write that arrives less than this
            many seconds after the last 0x96 query answered (the real BMS does that while it is
            still busy answering the query, although it acknowledges the write)
        """
        self._callbacks = {}
        self.ignore_writes = ignore_writes
        self.misroute = dict(misroute or {})
        self.drop_writes_within_s = drop_writes_within_s
        self._last_query_t = None  # time.monotonic() of the last 0x96 answered
        self.stream = stream
        self._streaming = False
        self.writes = []  # (register, value bytes) of every settings write received
        self.logger = get_logger()
        self.MSGS = [
            # settings 0x01:
            b'U\xaa\xeb\x90\x01\xd3X\x02\x00\x00(\n\x00\x00Z\n\x00\x00\xac\r\x00\x00\x16\r\x00\x00\n\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xc4\t\x00\x00\xa0\x86\x01\x00\x1e\x00\x00\x00<\x00\x00\x00\xc0\xd4\x01\x00,\x01\x00\x00<\x00\x00\x00<\x00\x00\x00\xd0\x07\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\x00\x00\x00\x002\x00\x00\x00\x84\x03\x00\x00\xbc\x02\x00\x00\x08\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x0082\x04\x00\xdc\x05\x00\x00\xb8\x0b\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x83\xaaU\x90\xeb\xc8\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00D',


            b'U\xaa\xeb\x90\x02\xb9"\r$\r%\r*\r$\r"\r%\r"\r\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x00\x00$\r\x08\x00\x03\x016\x005\x004\x005\x004\x004\x005\x004\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x1fi\x00\x00v\x04\x05\x00\xbb/\x00\x00\x0c\x01\xf8\x00\x1b\x01\x00\x00\x00\x00\x00A,\xc3\x02\x0082\x04\x00\x00\x00\x00\x00\x9dV\x00\x00d\x00\xa8\x02\x00\xfd/\x00\x01\x01\xd2\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x07\x00\x01\x00\x00\x00C\x048\x00\x00\x00\xf4\xaaA@\x00\x00\x00\x00\xe2\x04\xd40\x00\x00\x00\x01\x00\x05\x00\x00#)\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00='
            if not is_new_11x else
            #b"U\xaa\xeb\x90\x02S\x07\r\x07\r\x07\r\x08\r\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x0f\x00\x00\x00\x07\r\x01\x00\x00\x02'\x00'\x00&\x00&\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xe8\x00\x00\x00\x00\x00\x1c4\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xc7\x00\xc5\x00\x00\x00\x00\x00\x00\x00\x00a\x02%\x04\x00\xc0E\x04\x00\x05\x00\x00\x00J\x04\x18\x00d\x00\x00\x00K\x8c'\x00\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x01\x00\x00\x00\x9d\x03\x00\x00\x00\x00:\xccA@\x00\x00\x00\x006\x05\x00\x00\x00\x01\x00\x01\x00\x05\x00\x00Z\x03\n\x00\x00\x00\x00\x00\xe8\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xfe\xff\x7f\xdc/\x01\x01\x01\x00\x00\x00\x00J"
            b'U\xaa\xeb\x90\x02f\x13\r\x12\r\x12\r\x0f\r\x12\r\x14\r\x13\r\x12\r\x12\r\x12\r\x15\r\x13\r\x15\r\x14\r\x13\r\x13\r\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x00\x00\x12\r\x05\x00\x05\x0f>\x005\x005\x005\x005\x006\x003\x003\x003\x003\x003\x003\x003\x004\x004\x003\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb0\x00\x00\x00\x00\x00%\xd1\x00\x00\x0b\x94\x03\x00\x1c\x11\x00\x00\xaa\x00\xaa\x00\x00\x00\x00\x00\x00\x00\x00D\xa8(\x03\x00\x80\xa3\x04\x00:\x00\x00\x00\x14\x95\x0f\x01d\x00\x00\x00%\xe8\xe1\x00\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x01\x00\x00\x00\x13\x04\x15\x00\x00\x00\x8d\xfd@@\x00\x00\x00\x00\xea\x14\x00\x00\x00\x01\x00\x01\x00\x05\x00\x00u\xcb0\x00\x00\x00\x00\x00\xb0\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xfe\xff\x7f\xdc/\x01\x01\x01\x00\x00\x00\x00\x8f'
        ]
        # MSGS[0] is the 300-byte settings frame followed by a 20-byte command echo
        self._settings = bytearray(settings_frame if settings_frame is not None else self.MSGS[0][:300])

        from bmslib.models.jikong import JKBt
        self.services = [
            dotdict(uuid=JKBt.SERVICE_UUID, characteristics=[
                dotdict(uuid=JKBt.CHAR_UUID, properties='write,notify', handle=2, descriptors=[])
            ])
        ]

    async def start_notify(self, char_specifier, callback: Callable[[int, bytearray], None]):
        self._callbacks[char_specifier] = callback

    async def write_gatt_char(self, char_specifier, data: Union[bytes, bytearray, memoryview],
                              response: bool = False, ):
        crc = data[-1]
        data = bytes(data[:-1])
        from bmslib.models.jikong import calc_crc
        assert calc_crc(data) == crc

        notify = lambda msg: self._callbacks['0000ffe1-0000-1000-8000-00805f9b34fb'](self, bytes(msg))

        if data.startswith(b'\xaaU\x90\xeb\x97'):
            # device info
            self.logger.info('dummy query device info')
            notify(self.DEVICE_INFO)
        elif data.startswith(b'\xaaU\x90\xeb\x96'):
            self.logger.info('dummy subscribe')
            notify(self._settings)  # answer with the current (possibly patched) settings frame
            self._last_query_t = time.monotonic()

            if self.stream and not self._streaming:
                self._streaming = True

                def send_data():
                    while True:
                        time.sleep(1)
                        notify(bytes(self._settings) + self.MSGS[0][300:])  # keep the echo tail
                        for msg in self.MSGS[1:]:
                            notify(msg)

                Thread(target=send_data, daemon=True).start()
        elif data.startswith(b'\xaaU\x90\xeb') and data[4] in self._settings_by_register():
            self._apply_write(self._settings_by_register()[data[4]], data[6:19])
        else:
            raise Exception("JK dummy received unrecognized msg %s" % data)

    @property
    def settings_frame(self) -> bytes:
        return bytes(self._settings)

    @staticmethod
    def _settings_by_register():
        from bmslib.models.jk_settings import SETTINGS
        return {s.register: s for s in SETTINGS.values()}

    def _apply_write(self, setting, payload: bytes):
        from bmslib.models.jikong import calc_crc
        value = bytes(payload[:setting.length])
        self.writes.append((setting.register, value))
        self.logger.info('dummy write %s (0x%02x) = %s', setting.name, setting.register, value.hex())
        if (self.drop_writes_within_s and self._last_query_t is not None
                and time.monotonic() - self._last_query_t < self.drop_writes_within_s):
            self.logger.info('dummy busy answering a settings query: write dropped')
            return
        if setting.register in self.misroute:
            from bmslib.models.jk_settings import SETTINGS
            setting = SETTINGS[self.misroute[setting.register]]
        if self.ignore_writes or setting.read is None:
            return
        if setting.kind == 'switch':
            offset, mask = setting.read
            if value[0]:
                self._settings[offset] |= mask
            else:
                self._settings[offset] &= ~mask & 0xFF
        else:
            offset, width, signed = setting.read
            raw = int.from_bytes(value, 'little', signed=setting.signed)
            self._settings[offset:offset + width] = raw.to_bytes(width, 'little', signed=signed)
        self._settings[-1] = calc_crc(self._settings[:-1])


class JBDDummy:
    def __init__(self):
        self._callbacks = {}

    async def start_notify(self, char_specifier, callback: Callable[[int, bytearray], None]):
        self._callbacks[char_specifier] = callback

    async def write_gatt_char(self, char_specifier, data: Union[bytes, bytearray, memoryview],
                              response: bool = False, ):
        if data == b'\xdd\xa5\x03\x00\xff\xfdw':
            msg = bytearray.fromhex('dd03001b0a50fda4b717dac000002cf300000000000016540308020b7d0b77f8e277')
            self._callbacks['0000ff01-0000-1000-8000-00805f9b34fb'](self, bytes(msg))
        pass
