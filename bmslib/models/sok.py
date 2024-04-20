"""
SOK BMS protocol reverse engineered by @zuccaro Tony Zuccaro 
from ABC BMS Android app (com.sjty.sbs_bms). 

References
- https://github.com/Louisvdw/dbus-serialbattery/issues/350#issuecomment-1500658941

svc_id    = '0000ffe0-0000-1000-8000-00805f9b34fb'
notify_id = '0000ffe1-0000-1000-8000-00805f9b34fb'
tx_id     = '0000ffe2-0000-1000-8000-00805f9b34fb'

cmd_name       = [ 0xee, 0xc0, 0x00, 0x00, 0x00 ]
cmd_info       = [ 0xee, 0xc1, 0x00, 0x00, 0x00 ]
cmd_detail     = [ 0xee, 0xc2, 0x00, 0x00, 0x00 ]
cmd_setting    = [ 0xee, 0xc3, 0x00, 0x00, 0x00 ]
cmd_protection = [ 0xee, 0xc4, 0x00, 0x00, 0x00 ]
cmd_break      = [ 0xdd, 0xc0, 0x00, 0x00, 0x00 ]
"""
import asyncio
import logging
import platform
import struct
import statistics

from bmslib import FuturesPool
from bmslib.bms import BmsSample
from bmslib.bt import BtBms


def get_str(ubit, uuid):
    """ reads utf8 string from specified bluetooth uuid """
    return ''.join(bytes(ubit.char_read(uuid)).decode('UTF-8'))


def unpack(pattern, data):
    """ slightly simpler unpack call """
    return struct.unpack(pattern, data)[0]


def getBeUint4(data, offset):
    """ gets big-endian unsigned integer """
    return unpack('>I', bytes(data[offset:offset+4]))


def getBeUint3(data, offset):
    """ reads 3b big-endian unsigned int  """
    return unpack('>I', bytes([0]+data[offset:offset+3]))


def getLeInt3(data, offset):
    """ reads 3b little-endian signed int """
    return unpack('<i', bytes([0] + data[offset:offset+3]))


def getLeShort(data, offset):
    """ reads little-endian signed short """
    return unpack('<h', bytes(data[offset:offset+2]))


def getLeUShort(data, offset):
    """ reads little-endian unsigned short """
    return unpack('<H', bytes(data[offset:offset+2]))


def minicrc(data):
    """ computes crc8 of specified data"""
    i = 0
    for b in data:
        i ^= b & 255
        for i2 in range(0, 8):
            i = (i >> 1) ^ 140 if (i & 1) != 0 else i >> 1
    return i


def _sok_command(command: int):
    data = [0xee, command, 0x00, 0x00, 0x00]
    data2 = data + [minicrc(data)]
    logging.debug(f'SOK: Formatting command [{data2}]')
    return bytes([0xEE, command, 0x00, 0x00, 0x00])


class SokBt(BtBms):
    UUID_RX = '0000ffe1-0000-1000-8000-00805f9b34fb'
    UUID_TX = '0000ffe2-0000-1000-8000-00805f9b34fb'
    TIMEOUT = 10

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
            await self.client.write_gatt_char(self.UUID_TX, data=_sok_command(cmd))
            return await self._fetch_futures.wait_for(cmd, self.TIMEOUT)

    async def fetch(self) -> BmsSample:

        buf = await self._q(cmd=0xC1)  # info
        logging.debug(f'SOK: Received [{bytes(buf).hex().upper()}]')
        # this is not accurate, find out why
        # self.volts = (getLeInt3(value, 2) * 4) / 1000**2
        # ma = getLeInt3(buf, 5) / 1000**2
        # num_cycles = (struct.unpack('<H',bytes(buf[14:16]))[0])
        soc = struct.unpack('<H', bytes(buf[16:18]))[0]
        # ema = getLeInt3(buf, 8) / 1000 # not sure what this is
        current = getLeInt3(buf, 11) / 1000

        buf = await self._q(cmd=0xC0)  # name
        logging.debug(f'SOK: Received [{bytes(buf).hex().upper()}]')
        # name = bytes(buf[2:10]).decode('utf-8').rstrip()

        # buf = await self._q(cmd=0xCCF2)  # temps
        # logging.debug(f'SOK: Received [{bytes(buf).hex().upper()}]')
        # temp = getLeShort(buf, 5)

        buf = await self._q(cmd=0xC2)  # year, mv, hot
        logging.debug(f'SOK: Received [{bytes(buf).hex().upper()}]')
        # year = 2000 + buf[2]
        rated = getBeUint3(buf, 5) / 128
        # heater_on = getLeUShort(buf,8)

        buf = await self._q(cmd=0xC2)  # detail
        logging.debug(f'SOK: Received [{bytes(buf).hex().upper()}]')
        cells = [0, 0, 0, 0]
        for x in range(0, 4):
            cell = buf[2+(x*4)]
            cells[cell - 1] = getLeUShort(buf, 3+(x*4))
        voltage = (statistics.mean(cells)*4)/1000

        sample = BmsSample(
            voltage=voltage,
            current=current,
            soc=soc,
            capacity=rated
        )

        return sample

    async def fetch_voltages(self):
        buf = await self._q(cmd=0xCCF4)  # cell level voltages
        cells = [0, 0, 0, 0]
        for x in range(0, 4):
            cell = buf[2+(x*4)]
            cells[cell - 1] = getLeUShort(buf, 3+(x*4))
        return cells


async def main():
    mac_address = (
        "D6:6C:0A:61:14:30"
        if platform.system() != "Darwin"
        else "521087E3-F05D-BB20-5E63-CA46F91205FE"
    )
    bms = SokBt(mac_address, name='sok')
    await bms.connect()
    sample = await bms.fetch()
    print(sample)
    await bms.disconnect()

if __name__ == '__main__':
    asyncio.run(main())