"""

https://github.com/jblance/mpp-solar
https://github.com/jblance/jkbms
https://github.com/sshoecraft/jktool/blob/main/jk_info.c
https://github.com/syssi/esphome-jk-bms
https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp
https://github.com/PurpleAlien/jk-bms_grafana


fix connection abort:
- https://github.com/hbldh/bleak/issues/631 (use bluetoothctl !)
- https://github.com/hbldh/bleak/issues/666




"""
import asyncio
from struct import unpack
from typing import Dict

from bms import BmsSample
from bt import BtBms

def calc_crc(message_bytes):
    return sum(message_bytes) & 0xFF

def to_hex_str(data):
    return " ".join(map(lambda b: hex(b)[2:], data))

def _jk_command(address, value, length):

    frame =  bytes([0xAA,0x55, 0x90, 0xEB, address, length,
                  value[0], value[1], value[2], value[3]] + [0]*9)

    assert len(frame) == 19
    frame += bytes([calc_crc(frame)])
    return frame


MIN_RESPONSE_SIZE = 300
"""

primaryUuid         = '0000ffe0-0000-1000-8000-00805f9b34fb'  # noqa: E221
notifyUuid          = '0000ffe1-0000-1000-8000-00805f9b34fb'  # noqa: E221
writeUuid           = '0000ffe2-0000-1000-8000-00805f9b34fb'  # noqa: E221
readUuid            = '0000ffe3-0000-1000-8000-00805f9b34fb'  # noqa: E221
"""

class JKBt(BtBms):
    UUID_RX = "0000ffe1-0000-1000-8000-00805f9b34fb"
    UUID_TX = '0000ffe2-0000-1000-8000-00805f9b34fb'

    # UUID_RX = "f000ffc1-0451-4000-b000-000000000000"
    # UUID_TX = UUID_RX

    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._fetch_futures: Dict[int, asyncio.Future] = {}

    def _notification_handler(self, sender, data):
        print("bms msg({2}) {0}: {1}\n".format(sender, to_hex_str(data), len(data)))

        if data[0:4] == bytes([0x55, 0xAA, 0xEB, 0x90]): #  and len(self._buffer)
            self.logger.info("preamble, clear buf %s", self._buffer)
            self._buffer.clear()

        self._buffer += data

        if len(self._buffer) > MIN_RESPONSE_SIZE:
            crc_comp = calc_crc(self._buffer[0:MIN_RESPONSE_SIZE-1])
            crc_expected = self._buffer[MIN_RESPONSE_SIZE-1]
            if crc_comp != crc_expected:
                self.logger.error("crc check failed, %s != %s, %s", crc_comp, crc_expected, self._buffer)
            else:
                resp_type = self._buffer[4]
                self.logger.info('got response %d (len%d)', resp_type, len(self._buffer))
                fut = self._fetch_futures.pop(resp_type, None)
                if fut:
                    fut.set_result(self._buffer[:])

            self._buffer.clear()

    async def connect(self, timeout=20):
        """
        Connecting JK with bluetooth appears to require a prior bluetooth scan and discovery, otherwise the connectiong fails with
        `[org.bluez.Error.Failed] Software caused connection abort`. Maybe the scan triggers some wake up?
        :param timeout:
        :return:
        """
        import bleak
        scanner = bleak.BleakScanner()
        self.logger.debug("starting scan")
        await scanner.start()

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                if self.client.address not in discovered:
                    raise Exception('Device %s not discovered (%s)' % (self.client.address, discovered))

                self.logger.info("connect attempt %d", attempt)
                await super().connect(timeout=timeout)
                break
            except Exception as e:
                await self.client.disconnect()
                if attempt < 8:
                    self.logger.info('retry after error %s', e)
                    await asyncio.sleep(0.2 * (1.5 ** attempt))
                    attempt += 1
                else:
                    await scanner.stop()
                    raise

        await scanner.stop()

        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        self.logger.info("disconnect jk")
        await self.client.stop_notify(self.UUID_RX)
        self._fetch_futures.clear()
        await super().disconnect()


    async def _q(self, cmd, resp):
        assert cmd not in self._fetch_futures, "%s already waiting" % cmd
        self._fetch_futures[resp] = asyncio.Future()
        frame = _jk_command(cmd, bytes([0,0,0,0]), 0)
        self.logger.info("write %s", frame)
        await self.client.write_gatt_char(self.UUID_TX, data=frame)
        res = await asyncio.wait_for(self._fetch_futures[resp], self.TIMEOUT)
        # print('cmd', cmd, 'result', res)
        return res

    async def fetch(self) -> BmsSample:
        buf = await self._q(cmd=0x96, resp=0x01)

        # https://github.com/syssi/esphome-jk-bms/blob/fc6007433d3d69fdbc700c23af41a70ff8ed45db/components/jk_bms_ble/jk_bms_ble.cpp#L740

        num_cell = buf[114]
        capacity = int.from_bytes(buf[130:134], byteorder='little', signed=False) * 0.001

        num_temp = int.from_bytes(buf[22:23], 'big')

        sample = BmsSample(
            voltage=int.from_bytes(buf[0:2], byteorder='big', signed=True) / 100.0,
            current=-int.from_bytes(buf[2:4], byteorder='big', signed=True) / 100.0,

            charge=int.from_bytes(buf[4:6], byteorder='big', signed=True) / 100.,
            charge_full=int.from_bytes(buf[6:8], byteorder='big', signed=True) / 100,

            num_cycles=int.from_bytes(buf[8:10], byteorder='big', signed=True),

            temperatures=[(int.from_bytes(buf[23 + i * 2:i * 2 + 25], 'big') - 2731) / 10 for i in range(num_temp)],

            # charge_enabled
            # discharge_enabled
        )

        # print(dict(num_cell=num_cell, num_temp=num_temp))

        # self.rawdat['P']=round(self.rawdat['Vbat']*self.rawdat['Ibat'], 1)
        # self.rawdat['Bal'] = int.from_bytes(self.response[12:14], byteorder='big', signed=False)

        product_date = int.from_bytes(buf[10:12], byteorder='big', signed=True)
        # productDate = convertByteToUInt16(data1: data[14], data2: data[15])

        return sample

    async def fetch_voltages(self):
        #buf = await self._q(cmd=0x96, resp=0x01) # 0x02
        buf = await self._q(cmd=0x03, resp=0x01)  # 0x02
        # buf = await self._q(cmd=0x97, resp=0x03)  # 0x02
        #buf = await self._q(cmd=0xFF, resp=0x03)  # 0x02
        print(buf)
        await asyncio.sleep(10)
        print(
            int.from_bytes(buf[5:7], 'big'),
            int.from_bytes(buf[6:8], 'big')
        )
        num_cell = int(buf[3] / 2)
        voltages = [(int.from_bytes(buf[4 + i * 2:i * 2 + 6], 'big')) for i in range(num_cell)]
        return voltages



async def main():
    mac_address = 'C8:47:8C:F7:AD:B4'
    bms = JKBt(mac_address, name='jk')
    async with bms:
        voltages = await bms.fetch_voltages()
    #await bms.connect()

    print(voltages)
    # sample = await bms.fetch()
    # print(sample)
    # await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())



"""
pi@raspberrypi:~/batmon-ha $ python3 tools/service_explorer.py "C8:47:8C:F7:AD:B4"
INFO:__main__:Connected: True
INFO:__main__:[Service] 00001801-0000-1000-8000-00805f9b34fb (Handle: 10): Generic Attribute Profile
INFO:__main__:	[Characteristic] 00002a05-0000-1000-8000-00805f9b34fb (Handle: 11): Service Changed (read,indicate), Value: b'\x01\x00\xff\xff'
INFO:__main__:		[Descriptor] 00002902-0000-1000-8000-00805f9b34fb (Handle: 13): Client Characteristic Configuration) | Value: b'\x02\x00'
INFO:__main__:[Service] 0000ffe0-0000-1000-8000-00805f9b34fb (Handle: 14): Vendor specific
INFO:__main__:	[Characteristic] 0000ffe2-0000-1000-8000-00805f9b34fb (Handle: 15): Vendor specific (write-without-response), Value: None
INFO:__main__:	[Characteristic] 0000ffe1-0000-1000-8000-00805f9b34fb (Handle: 17): Vendor specific (write-without-response,write,notify), Value: None
INFO:__main__:		[Descriptor] 00002902-0000-1000-8000-00805f9b34fb (Handle: 19): Client Characteristic Configuration) | Value: b'\x00\x00'
INFO:__main__:[Service] 0000180a-0000-1000-8000-00805f9b34fb (Handle: 20): Device Information
INFO:__main__:	[Characteristic] 00002a29-0000-1000-8000-00805f9b34fb (Handle: 21): Manufacturer Name String (read), Value: b'BEKEN SAS'
INFO:__main__:	[Characteristic] 00002a24-0000-1000-8000-00805f9b34fb (Handle: 23): Model Number String (read), Value: b'BK-BLE-1.0'
INFO:__main__:	[Characteristic] 00002a25-0000-1000-8000-00805f9b34fb (Handle: 25): Serial Number String (read), Value: b'1.0.0.0-LE'
INFO:__main__:	[Characteristic] 00002a27-0000-1000-8000-00805f9b34fb (Handle: 27): Hardware Revision String (read), Value: b'1.0.0'
INFO:__main__:	[Characteristic] 00002a26-0000-1000-8000-00805f9b34fb (Handle: 29): Firmware Revision String (read), Value: b'6.1.2'
INFO:__main__:	[Characteristic] 00002a28-0000-1000-8000-00805f9b34fb (Handle: 31): Software Revision String (read), Value: b'6.3.0'
INFO:__main__:	[Characteristic] 00002a23-0000-1000-8000-00805f9b34fb (Handle: 33): System ID (read), Value: b'\x124V\xff\xfe\x9a\xbc\xde'
INFO:__main__:	[Characteristic] 00002a2a-0000-1000-8000-00805f9b34fb (Handle: 35): IEEE 11073-20601 Regulatory Cert. Data List (read), Value: b'\xff\xee\xdd\xcc\xbb\xaa'
INFO:__main__:	[Characteristic] 00002a50-0000-1000-8000-00805f9b34fb (Handle: 37): PnP ID (read), Value: b'\x02^\x04@\x00\x00\x03'
INFO:__main__:[Service] 0000180f-0000-1000-8000-00805f9b34fb (Handle: 39): Battery Service
INFO:__main__:	[Characteristic] 00002a19-0000-1000-8000-00805f9b34fb (Handle: 40): Battery Level (read,notify), Value: b'\x00'
INFO:__main__:		[Descriptor] 00002902-0000-1000-8000-00805f9b34fb (Handle: 42): Client Characteristic Configuration) | Value: b'\x01\x00'
INFO:__main__:[Service] f000ffc0-0451-4000-b000-000000000000 (Handle: 43): Unknown
INFO:__main__:	[Characteristic] f000ffc1-0451-4000-b000-000000000000 (Handle: 44): Unknown (write-without-response,write,notify), Value: None
INFO:__main__:		[Descriptor] 00002902-0000-1000-8000-00805f9b34fb (Handle: 46): Client Characteristic Configuration) | Value: b'\x00\x00'
INFO:__main__:		[Descriptor] 00002901-0000-1000-8000-00805f9b34fb (Handle: 47): Characteristic User Description) | Value: b'Img Identify\x00'
INFO:__main__:	[Characteristic] f000ffc2-0451-4000-b000-000000000000 (Handle: 48): Unknown (write-without-response,write,notify), Value: None
INFO:__main__:		[Descriptor] 00002902-0000-1000-8000-00805f9b34fb (Handle: 50): Client Characteristic Configuration) | Value: b'\x00\x00'
INFO:__main__:		[Descriptor] 00002901-0000-1000-8000-00805f9b34fb (Handle: 51): Characteristic User Description) | Value: b'Img Block\x00'

"""


"""

INFO     [jikong.py:91] write b'\xaaU\x90\xeb\x96\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10'
bms msg 17: bytearray(b'U\xaa\xeb\x90\x01\xdaX\x02\x00\x00(\n\x00\x00Z\n\x00\x00\xac\r\x00\x00\x16\r\x00\x00\n\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xc4\t\x00\x00\xa0\x86\x01\x00\x1e\x00\x00\x00<\x00\x00\x00\xc0\xd4\x01\x00,\x01\x00\x00<\x00\x00\x00<\x00\x00\x00\xd0\x07\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\x00\x00\x00\x002\x00\x00\x00\x84\x03\x00\x00\xbc\x02\x00\x00\x08\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00')
INFO     [jikong.py:58] preamble, clear buf bytearray(b'U\xaa\xeb\x90\x01\xdaX\x02\x00\x00(\n\x00\x00Z\n\x00\x00\xac\r\x00\x00\x16\r\x00\x00\n\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xc4\t\x00\x00\xa0\x86\x01\x00\x1e\x00\x00\x00<\x00\x00\x00\xc0\xd4\x01\x00,\x01\x00\x00<\x00\x00\x00<\x00\x00\x00\xd0\x07\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\x00\x00\x00\x002\x00\x00\x00\x84\x03\x00\x00\xbc\x02\x00\x00\x08\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00')
bms msg 17: bytearray(b'\x00\x0082\x04\x00\xdc\x05\x00\x00\xb8\x0b\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
bms msg 17: bytearray(b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x8a\xaaU\x90\xeb\xc8\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00D')
ERROR    [jikong.py:67] crc check failed, b'\x8a' != 138, bytearray(b'U\xaa\xeb\x90\x01\xdaX\x02\x00\x00(\n\x00\x00Z\n\x00\x00\xac\r\x00\x00\x16\r\x00\x00\n\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xc4\t\x00\x00\xa0\x86\x01\x00\x1e\x00\x00\x00<\x00\x00\x00\xc0\xd4\x01\x00,\x01\x00\x00<\x00\x00\x00<\x00\x00\x00\xd0\x07\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\xa4\x01\x00\x00\x90\x01\x00\x00\x00\x00\x00\x002\x00\x00\x00\x84\x03\x00\x00\xbc\x02\x00\x00\x08\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x0082\x04\x00\xdc\x05\x00\x00\xb8\x0b\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x8a\xaaU\x90\xeb\xc8\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00D')
INFO     [jikong.py:81] disconnect jk

"""