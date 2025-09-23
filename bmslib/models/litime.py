"""
Based on jbd.py 

Eddited using this repos as hint
https://github.com/calledit/LiTime_BMS_bluetooth/blob/main/index.html
https://github.com/chadj/litime-bluetooth-battery


"""
import asyncio

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


def _litime_command(command: int):
    return bytes([0x00, 0x00, 0x04, 0x01, 0x13, 0x55, 0xAA, 0x17])


class LitimeBt(BtBms):
    UUID_RX = '0000ffe1-0000-1000-8000-00805f9b34fb'
    UUID_TX = '0000ffe2-0000-1000-8000-00805f9b34fb'
    TIMEOUT = 5

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._switches = None
        self._last_response = None

    def _notification_handler(self, sender, data):
        self._buffer = data
        self._last_response = data
        self._fetch_futures.set_result(0x01, data)
 
    async def connect(self, **kwargs):
        #await super().connect(**kwargs)
        try:
            await super().connect(**kwargs)
        except Exception as e:
            self.logger.info("normal connect failed (%s), connecting with scanner", e)
            await self._connect_with_scanner(**kwargs)

        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        await self.client.stop_notify(self.UUID_RX)
        await super().disconnect()

    async def _q(self, cmd):
        with self._fetch_futures.acquire(cmd):
            await self.client.write_gatt_char(self.UUID_TX, data=_litime_command(cmd))
            return await self._fetch_futures.wait_for(cmd, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        buf = await self._q(cmd=0x01)
        #self.logger.info('Data read:')
        #self.logger.info(buf)
        cels_temperature = int.from_bytes(buf[52:54], byteorder='little',signed=True)
        #self.logger.info('Parsed Cell Temp:')
        #self.logger.info(cels_temperature)
        sample = BmsSample(
            voltage=int.from_bytes(buf[12:16], byteorder='little') / 1000,
            current=-int.from_bytes(buf[48:52], byteorder='little',signed=True) / 1000,
            charge = int.from_bytes(buf[62:64], byteorder='little') / 100,
            capacity=int.from_bytes(buf[64:66], byteorder='little') / 100,
            soc=int.from_bytes(buf[90:92], byteorder='little'),
            num_cycles=int.from_bytes(buf[96:100], byteorder='little'),
            temperatures= [cels_temperature],
            mos_temperature = int.from_bytes(buf[54:56], byteorder='little'),
            cycle_capacity = int.from_bytes(buf[100:104], byteorder='little'),
            switches= None
        )

        self._switches = None

        # print(dict(num_cell=num_cell, num_temp=num_temp))

        # self.rawdat['P']=round(self.rawdat['Vbat']*self.rawdat['Ibat'], 1)
        # self.rawdat['Bal'] = int.from_bytes(self.response[12:14], byteorder='big', signed=False)

        #product_date = int.from_bytes(buf[10:12], byteorder='big', signed=True)
        # productDate = convertByteToUInt16(data1: data[14], data2: data[15])
        return sample

    async def fetch_voltages(self):
        cell_volts = []
        for x in range(16):
            offset = 16 + x * 2
            cell_volt = int.from_bytes(self._buffer[offset:offset + 2], byteorder='little')
            if cell_volt != 0:
                cell_volts.append(cell_volt)
        return cell_volts

    async def set_switch(self, switch: str, state: bool):
        self.logger.info("No swithes")

    def debug_data(self):
        return self._last_response


async def main():
    # mac_address = ''
    mac_address = 'A4:C1:38:44:48:E7'
    # serial_service = ''

    bms = LitimeBt(mac_address, name='litime')
    await bms.connect()
    voltages = await bms.fetch_voltages()
    print(voltages)
    # sample = await bms.fetch()
    # print(sample)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
