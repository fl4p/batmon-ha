"""
Other Daly protocol

References
- https://github.com/roccotsi2/esp32-smart-bms-simulation

-  https://github.com/tomatensaus/python-daly-bms

"""
import asyncio

from bmslib import FuturesPool
from .bms import BmsSample
from .bt import BtBms


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

    def _notification_handler(self, sender, data):
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
            await self.client.write_gatt_char(self.UUID_TX, data=_jbd_command(cmd))
            return await self._fetch_futures.wait_for(cmd, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        # binary reading
        #  https://github.com/roccotsi2/esp32-smart-bms-simulation

        buf = await self._q(cmd=bytes.fromhex("D2 03 00 00 00 3E D7 B9"))
        buf = buf[4:]

        num_cell = int.from_bytes(buf[21:22], 'big')
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
        buf = await self._q(cmd=0x04)
        num_cell = int(buf[3] / 2)
        voltages = [(int.from_bytes(buf[4 + i * 2:i * 2 + 6], 'big')) for i in range(num_cell)]
        return voltages

    async def set_switch(self, switch: str, state: bool):

        assert switch in {"charge", "discharge"}

        # see https://wiki.jmehan.com/download/attachments/59114595/JBD%20Protocol%20English%20version.pdf?version=1&modificationDate=1650716897000&api=v2
        #
        def jbd_checksum(cmd, data):
            crc = 0x10000
            for i in (data + bytes([len(data), cmd])):
                crc = crc - int(i)
            return crc.to_bytes(2, byteorder='big')

        def jbd_message(status_bit, cmd, data):
            return bytes([0xDD, status_bit, cmd, len(data)]) + data + jbd_checksum(cmd, data) + bytes([0x77])

        if not self._switches:
            await self.fetch()

        new_switches = {**self._switches, switch: state}
        switches_sum = sum(new_switches.values())
        if switches_sum == 2:
            tc = 0x00  # all on
        elif switches_sum == 0:
            tc = 0x03  # all off
        elif switch == "charge" and not state:
            tc = 0x01  # charge off
        else:
            tc = 0x02  # charge on, discharge off

        data = jbd_message(status_bit=0x5A, cmd=0xE1, data=bytes([0x00, tc]))  # all off
        self.logger.info("send switch msg: %s", data)
        await self.client.write_gatt_char(self.UUID_TX, data=data)


async def main():
    # mac_address = 'A3161184-6D54-4B9E-8849-E755F10CEE12'
    mac_address = 'A4:C1:38:44:48:E7'
    # serial_service = '0000ff00-0000-1000-8000-00805f9b34fb'

    bms = JbdBt(mac_address, name='jbd')
    await bms.connect()
    voltages = await bms.fetch_voltages()
    print(voltages)
    # sample = await bms.fetch()
    # print(sample)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
