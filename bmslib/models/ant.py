"""
ANT BMS

https://github.com/syssi/esphome-ant-bms/blob/main/components/ant_bms_ble/ant_bms_ble.cpp
https://github.com/juamiso/ANT_BMS
https://github.com/Sgw32/BMSCtl


INFO:__main__:Connecting 9AA68C04-9C48-4FAD-7798-13ABB4878996
INFO:__main__:Connected: True
INFO:__main__:[Service] 0000ffe0-0000-1000-8000-00805f9b34fb (Handle: 14): Vendor specific
INFO:__main__:	[Characteristic] 0000ffe1-0000-1000-8000-00805f9b34fb (Handle: 15): Vendor specific (write-without-response,write,notify), Value: None
INFO:__main__:		[Descriptor] 00002902-0000-1000-8000-00805f9b34fb (Handle: 17): Client Characteristic Configuration) | Value: b''
INFO:__main__:	[Characteristic] 0000ffe2-0000-1000-8000-00805f9b34fb (Handle: 18): Vendor specific (write-without-response,write), Value: None

"""
import asyncio
import enum
import math

import crcmod as crcmod

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms
from bmslib.util import to_hex_str

crc16_modbus = crcmod.mkCrcFun(0x18005, rev=True, initCrc=0xFFFF, xorOut=0x0000)


def calc_crc16(data: bytes):
    i = crc16_modbus(data)
    return [i & 0xff, (i >> 8) & 0xff]


class AntCommandFuncs(enum.Enum):
    Status = 0x01
    DeviceInfo = 0x02
    WriteRegister = 0x51


def _ant_command(func: AntCommandFuncs, addr, value):
    """
    :param func: uint8
    :param addr: uint16
    :param value: uint8
    :return:
    """
    frame = bytes([0x7e, 0xa1, func.value, addr & 0xff, (addr >> 8) & 0xff, value])
    # frame = bytes([0x7e, 0xa1, func.value] + list(int.to_bytes(addr, length=2, byteorder='little')) + [value])
    crc = calc_crc16(frame[1:])
    frame += bytes(crc + [0xaa, 0x55])
    return frame


class AntBt(BtBms):
    CHAR_UUID = '0000ffe1-0000-1000-8000-00805f9b34fb'  # Handle 0x10
    TIMEOUT = 16
    WRITE_REGISTER = 0x51

    TEMPERATURE_STEP = 1 # it sends out noisy data in between
    TEMPERATURE_SMOOTH = 40

    def __init__(self, address, **kwargs):
        super().__init__(address, _uses_pin=False, **kwargs)
        self._buffer = bytearray()
        self._switches = None
        self._last_response = None
        self._voltages = []

    def _notification_handler(self, sender, data: bytes):

        # MAX_RESPONSE_SIZE = 152

        # print("bms msg {0}: {1} {2}".format(sender, to_hex_str(data), data))

        if data.startswith(b'\x7E\xA1'):
            self._buffer.clear()

        self._buffer += data

        if self._buffer.endswith(b'\x55'):
            buf = self._buffer
            func = buf[2]
            data_len = buf[5]
            frame_len = 6 + data_len + 4

            if len(buf) < frame_len:
                self.logger.warning('Unexpected size: header says %d, got %d bytes', frame_len, len(buf))
                buf.clear()
                return

            crc = calc_crc16(buf[1:1 + frame_len - 5])
            crc_exp = list(buf[frame_len - 4:frame_len - 2])

            self._last_response = buf[:]
            self._buffer.clear()

            if crc != crc_exp:
                self.logger.warning('CRC16 error: %s != %s (expected)', crc, crc_exp)
            else:
                self._fetch_futures.set_result(func, self._last_response)

    async def connect(self, timeout=20, **kwargs):
        # await super().connect(**kwargs)
        try:
            await super().connect(timeout=6)
        except Exception as e:
            self.logger.info("normal connect failed (%s), connecting with scanner", str(e) or type(e))
            await self._connect_with_scanner(timeout=timeout)

        await self.start_notify(self.CHAR_UUID, self._notification_handler)

    async def disconnect(self):
        await self.client.stop_notify(self.CHAR_UUID)
        await super().disconnect()

    async def _q(self, cmd: AntCommandFuncs, addr, val, resp_code):
        with await self._fetch_futures.acquire_timeout(resp_code, timeout=self.TIMEOUT/2):
            await self.client.write_gatt_char(self.CHAR_UUID, data=_ant_command(cmd, addr, val))
            return await self._fetch_futures.wait_for(resp_code, self.TIMEOUT)

    async def fetch_device_info(self) -> DeviceInfo:
        buf: bytearray = await self._q(AntCommandFuncs.DeviceInfo, 0x026c, 0x20, resp_code=0x12)
        hw = bytearray.decode(buf[6:6 + 16].strip(b'\0'), 'utf-8')
        dev = DeviceInfo(
            mnf="ANT",
            model='ANT-' + hw,
            hw_version=hw,
            sw_version=bytearray.decode(buf[22:22 + 16].strip(b'\0'), 'utf-8'),
            name=None,
            sn=None,
        )
        return dev

    async def fetch(self) -> BmsSample:
        # data = bytearray(b'~\xa1\x11\x00\x00~\x05\x01\x02\x08\x02\x00\x00\x00\x00\x00\x00\x00\x01\x00B\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xd4\r\xd5\r\xd5\r\xd5\r\xd5\r\xd4\r\xd5\r\xd5\r\xd8\xff\xd8\xff\x1c\x00\x1d\x00\x11\x0b\x00\x00d\x00d\x00\x01\x02\x00\x00\x00\xe1\xf5\x05\x00\xe1\xf5\x05\xa52\x00\x00\x00\x00\x00\x00\xff\x97\x01\x00\x00\x00\x00\x00\xd5\r\x02\x00\xd4\r\x01\x00\x01\x00\xd4\r\xf8\xff\x82\x00\x00\x00\xab\x02\xf2\xfa\x10\x00\x00\x00:e\x00\x00\x1f\x00\x00\x00\xfab\x00\x00\x11\xc3\xaaU')
        data = await self._q(AntCommandFuncs.Status, 0x0000, 0xbe, resp_code=0x11)

        u16 = lambda i: int.from_bytes(data[i:(i + 2)], byteorder='little', signed=False)
        i16 = lambda i: int.from_bytes(data[i:(i + 2)], byteorder='little', signed=True)
        u32 = lambda i: int.from_bytes(data[i:(i + 4)], byteorder='little', signed=False)
        i32 = lambda i: int.from_bytes(data[i:(i + 4)], byteorder='little', signed=True)

        num_temp = data[8]
        num_cell = data[9]
        offset = 34

        self._voltages = [u16(i * 2 + offset) for i in range(num_cell)]
        offset += num_cell * 2

        temperatures = [u16(i * 2 + offset) for i in range(num_temp)]
        temperatures = [t if t != 65496 else math.nan for t in temperatures]
        offset += num_temp * 2

        mos_temp = u16(offset)
        offset += 2

        # balancer_temp = u16(offset)
        offset += 2

        voltage = u16(offset) * 0.01
        offset += 2

        current = i16(offset) * 0.1
        offset += 2

        soc = u16(offset)
        offset += 2

        # soh = u16(offset)  # state of health
        offset += 2

        # dsg mos state
        switch_dsg = data[offset]
        offset += 1

        # charge mos state
        switch_chg = data[offset]
        offset += 1

        # bal state
        offset += 1

        # reserved ?
        offset += 1

        capacity = u32(offset) * 0.000001
        offset += 4

        charge = u32(offset) * 0.000001
        offset += 4

        cycle_charge = u32(offset) * 0.001
        offset += 4

        # power = i32(offset)
        offset += 4

        sample = BmsSample(
            voltage=voltage,
            current=current,
            # power=
            charge=charge,
            capacity=capacity,
            cycle_capacity=cycle_charge,
            # num_cycles=0,
            soc=soc,

            temperatures=temperatures,
            mos_temperature=mos_temp,

            switches=dict(
                discharge=switch_dsg == 1,
                charge=switch_chg == 1,
            ),

            # charge_enabled
            # discharge_enabled
        )
        # self._switches = dict(sample.switches)

        return sample

    async def fetch_voltages(self):
        return self._voltages

    async def set_switch(self, switch: str, state: bool):
        register_onoff = dict(charge=[0x0006, 0x0004], discharge=[0x0003, 0x0001], balance=[0x000D, 0x000E],
                              buzzer=[0x001E, 0x001F])
        addr = register_onoff[switch][0 if state else 1]
        await self.client.write_gatt_char(self.CHAR_UUID, data=_ant_command(AntCommandFuncs.WriteRegister, addr, 0))

    def debug_data(self):
        return self._last_response


async def main():
    # mac_address = '9AA68C04-9C48-4FAD-7798-13ABB4878996'
    mac_address = '08E27970-3DA3-65C0-05E4-B1A8482449C5'

    # print(to_hex_str(_ant_command(AntCommandFuncs.DeviceInfo, 0x026c, 0x20)))

    bms = AntBt(mac_address, name='ant')

    await bms.connect()
    print('device info', await bms.fetch_device_info())

    sample = await bms.fetch()
    print(sample)

    voltages = await bms.fetch_voltages()
    print('voltage', voltages)

    # sample = await bms.fetch()
    # print(sample)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
