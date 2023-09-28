"""
Other Daly protocol

References
- https://github.com/roccotsi2/esp32-smart-bms-simulation

-  https://github.com/tomatensaus/python-daly-bms

"""

from bmslib import FuturesPool
from bmslib.bms import BmsSample
from bmslib.bt import BtBms


def _daly_command(command: int):
    return bytes([0xDD, 0xA5, command, 0x00, 0xFF, 0xFF - (command - 1), 0x77])


class JbdBt(BtBms):
    UUID_RX = '0000fff1-0000-1000-8000-00805f9b34fb'
    UUID_TX = '0000fff2-0000-1000-8000-00805f9b34fb'
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._fetch_futures = FuturesPool()
        self._switches = None

    def _notification_handler(self, _sender, data):
        self.logger.debug("ble data frame %s", data)
        self._buffer += data

        if self._buffer.endswith(b'w'):
            command = self._buffer[1]
            buf = self._buffer[:]
            self._buffer.clear()

            # print(command, 'buffer endswith w', self._buffer)
            self._fetch_futures.set_result(command, buf)

    async def connect(self, **kwargs):
        await super().connect(**kwargs)
        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        await self.client.stop_notify(self.UUID_RX)
        self._fetch_futures.clear()
        await super().disconnect()

    async def _q(self, cmd):
        with self._fetch_futures.acquire(cmd):
            # await self.client.write_gatt_char(self.UUID_TX, data=_jbd_command(cmd))
            return await self._fetch_futures.wait_for(cmd, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        # binary reading
        #  https://github.com/roccotsi2/esp32-smart-bms-simulation

        buf = await self._q(cmd=bytes.fromhex("D2 03 00 00 00 3E D7 B9"))
        buf = buf[4:]

        #num_cell = int.from_bytes(buf[21:22], 'big')
        num_temp = int.from_bytes(buf[22:23], 'big')

        mos_byte = int.from_bytes(buf[20:21], 'big')

        sample = BmsSample(
            voltage=int.from_bytes(buf[80:82], byteorder='big') / 10,
            current=(int.from_bytes(buf[82:84], byteorder='big', signed=True) - 30000) / 10,
            soc=int.from_bytes(buf[84:86], byteorder='big') / 10,

            charge=int.from_bytes(buf[4:6], byteorder='big', signed=True) / 100,
            capacity=int.from_bytes(buf[6:8], byteorder='big', signed=True) / 100,

            num_cycles=int.from_bytes(buf[8:10], byteorder='big', signed=True),

            temperatures=[(int.from_bytes(buf[23 + i * 2:i * 2 + 25], 'big') - 2731) / 10 for i in range(num_temp)],

            switches=dict(
                discharge=mos_byte == 2 or mos_byte == 3,
                charge=mos_byte == 1 or mos_byte == 3,
            ),

            # charge_enabled
            # discharge_enabled
        )

        self._switches = dict(sample.switches)

        # print(dict(num_cell=num_cell, num_temp=num_temp))

        # self.rawdat['P']=round(self.rawdat['Vbat']*self.rawdat['Ibat'], 1)
        # self.rawdat['Bal'] = int.from_bytes(self.response[12:14], byteorder='big', signed=False)

        product_date = int.from_bytes(buf[10:12], byteorder='big', signed=True)
        # productDate = convertByteToUInt16(data1: data[14], data2: data[15])

        return sample

    async def fetch_voltages(self):
        raise NotImplementedError()

    async def set_switch(self, switch: str, state: bool):
        pass


