"""

https://github.com/jblance/mpp-solar
https://github.com/jblance/jkbms
https://github.com/sshoecraft/jktool/blob/main/jk_info.c
https://github.com/syssi/esphome-jk-bms
https://github.com/PurpleAlien/jk-bms_grafana


fix connection abort:
- https://github.com/hbldh/bleak/issues/631 (use bluetoothctl !)
- https://github.com/hbldh/bleak/issues/666

"""
import asyncio

from . import FuturesPool
from .bms import BmsSample, DeviceInfo
from .bt import BtBms


def calc_crc(message_bytes):
    return sum(message_bytes) & 0xFF


def read_str(buf, offset, encoding='utf-8'):
    return buf[offset:buf.index(0x00, offset)].decode(encoding=encoding)


def to_hex_str(data):
    return " ".join(map(lambda b: hex(b)[2:], data))


def _jk_command(address, value: list):
    n = len(value)
    assert n <= 13, "val %s too long" % value
    frame = bytes([0xAA, 0x55, 0x90, 0xEB, address, n])
    frame += bytes(value)
    frame += bytes([0] * (13 - n))
    frame += bytes([calc_crc(frame)])
    return frame


MIN_RESPONSE_SIZE = 300
MAX_RESPONSE_SIZE = 320


class JKBt(BtBms):
    UUID_RX = "0000ffe1-0000-1000-8000-00805f9b34fb"
    UUID_TX = UUID_RX

    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._fetch_futures = FuturesPool()
        self._resp_table = {}
        self.num_cells = None

    def _buffer_crc_check(self):
        crc_comp = calc_crc(self._buffer[0:MIN_RESPONSE_SIZE - 1])
        crc_expected = self._buffer[MIN_RESPONSE_SIZE - 1]
        if crc_comp != crc_expected:
            self.logger.error("crc check failed, %s != %s, %s", crc_comp, crc_expected, self._buffer)
        return crc_comp == crc_expected

    def _notification_handler(self, sender, data):
        HEADER = bytes([0x55, 0xAA, 0xEB, 0x90])

        if data[0:4] == HEADER:  # and len(self._buffer)
            self.logger.debug("header, clear buf %s", self._buffer)
            self._buffer.clear()

        self._buffer += data

        self.logger.debug("bms msg(%d) (buf%d): %s\n", len(data), len(self._buffer), to_hex_str(data))

        if len(self._buffer) >= MIN_RESPONSE_SIZE:
            if len(self._buffer) > MAX_RESPONSE_SIZE:
                self.logger.warning('buffer longer than expected %d %s', len(self._buffer), self._buffer)

            crc_ok = self._buffer_crc_check()

            if not crc_ok and HEADER in self._buffer:
                idx = self._buffer.index(HEADER)
                self.logger.warning("crc check failed, header at %d, discarding start of %s", idx, self._buffer)
                self._buffer = self._buffer[idx:]
                crc_ok = self._buffer_crc_check()

            if not crc_ok:
                self.logger.error("crc check failed, discarding buffer %s", self._buffer)
            else:
                self._decode_msg(bytearray(self._buffer))
            self._buffer.clear()

    def _decode_msg(self, buf):
        resp_type = buf[4]
        self.logger.debug('got response %d (len%d)', resp_type, len(buf))
        self._resp_table[resp_type] = buf
        self._fetch_futures.set_result(resp_type, self._buffer[:])

    async def connect(self, timeout=20):
        """
        Connecting JK with bluetooth appears to require a prior bluetooth scan and discovery, otherwise the connectiong fails with
        `[org.bluez.Error.Failed] Software caused connection abort`. Maybe the scan triggers some wake up?
        :param timeout:
        :return:
        """

        try:
            await super().connect(timeout=4)
        except:
            self.logger.info("normal connect failed, connecting with scanner")
            await self._connect_with_scanner(timeout=timeout)

        await self.client.start_notify(self.UUID_RX, self._notification_handler)

        await self._q(cmd=0x97, resp=0x03)  # device info
        await self._q(cmd=0x96, resp=(0x02, 0x01))  # device state (resp 0x01 & 0x02)
        # after these 2 commands the bms will continuously send 0x02-type messages

        buf = self._resp_table[0x01]
        self.num_cells = buf[114]
        assert 0 < self.num_cells <= 24, "num_cells unexpected %s" % self.num_cells
        self.capacity = int.from_bytes(buf[130:134], byteorder='little', signed=False) * 0.001

    async def disconnect(self):
        await self.client.stop_notify(self.UUID_RX)
        self._fetch_futures.clear()
        await super().disconnect()

    async def _q(self, cmd, resp):
        with self._fetch_futures.acquire(resp):
            frame = _jk_command(cmd, [])
            self.logger.debug("write %s", frame)
            await self.client.write_gatt_char(self.UUID_TX, data=frame)
            return await self._fetch_futures.wait_for(resp, self.TIMEOUT)

    async def _write(self, address, value):
        frame = _jk_command(address, value)
        await self.client.write_gatt_char(self.UUID_TX, data=frame)

    async def device_info(self):
        # https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jkabstractprotocol.py
        # https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L1059
        buf = self._resp_table[0x03]
        return DeviceInfo(
            model=read_str(buf, 6),
            hw_version=read_str(buf, 6 + 16),
            sw_version=read_str(buf, 6 + 16 + 8),
            name=read_str(buf, 6 + 16 + 8 + 16),
            sn=read_str(buf, 6 + 16 + 8 + 16 + 40),
        )

    async def fetch(self, wait=True) -> BmsSample:

        """
        Decode JK02
        references
        * https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L360
        * https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py
        """

        if wait:
            with self._fetch_futures.acquire(0x02):
                await self._fetch_futures.wait_for(0x02, self.TIMEOUT)

        buf = self._resp_table[0x02]
        i16 = lambda i: int.from_bytes(buf[i:(i + 2)], byteorder='little', signed=True)
        u32 = lambda i: int.from_bytes(buf[i:(i + 4)], byteorder='little', signed=False)
        f32u = lambda i: u32(i) * 1e-3
        f32s = lambda i: int.from_bytes(buf[i:(i + 4)], byteorder='little', signed=True) * 1e-3

        return BmsSample(
            voltage=f32u(118),
            current=f32s(126),

            cycle_capacity=f32u(154),
            capacity=f32u(146),  # computed capacity (starts at self.capacity, which is user-defined),
            charge=f32u(142),  # "remaining capacity"

            temperatures=[i16(130) / 10, i16(132) / 10],
            mos_temperature=i16(134) / 10,
            balance_current=i16(138) / 1000,

            # 146 charge_full (see above)
            num_cycles=u32(150),
        )

        # TODO  154   4   0x3D 0x04 0x00 0x00    Cycle_Capacity       1.0

    async def fetch_voltages(self):
        """
        :return: list of cell voltages in mV
        """
        if self.num_cells is None:
            raise Exception("num_cells not set")
        buf = self._resp_table[0x02]
        voltages = [int.from_bytes(buf[(6 + i * 2):(6 + i * 2 + 2)], byteorder='little') for i in
                    range(self.num_cells)]
        return voltages


async def main():
    mac_address = 'C8:47:8C:F7:AD:B4'
    bms = JKBt(mac_address, name='jk')
    async with bms:
        while True:
            s = await bms.fetch(wait=True)
            print(s, 'I_bal=', s.balance_current, await bms.fetch_voltages())


if __name__ == '__main__':
    asyncio.run(main())
