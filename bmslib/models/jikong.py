"""
 JK, jikong

Manual
https://github.com/NEEY-electronic/JK/blob/JK-BMS/JKBMS%20INSTRUCTION.pdf

https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py
https://github.com/jblance/jkbms
https://github.com/sshoecraft/jktool/blob/main/jk_info.c
https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp
https://github.com/PurpleAlien/jk-bms_grafana


fix connection abort:
- https://github.com/hbldh/bleak/issues/631 (use bluetoothctl !)
- https://github.com/hbldh/bleak/issues/666

"""
import asyncio
import time
from collections import defaultdict
from typing import List, Callable, Dict, Tuple

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms
from bmslib.util import to_hex_str


def calc_crc(message_bytes):
    return sum(message_bytes) & 0xFF


def read_str(buf, offset, encoding='utf-8'):
    return buf[offset:buf.index(0x00, offset)].decode(encoding=encoding)


def _jk_command(address, value: list = ()):
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
    SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
    CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

    TIMEOUT = 8

    SOC_NOT_FULL_YET = 99.0  # when the gauge reaches 100% but no OV yet
    TEMPERATURE_STEP = 0.1
    TEMPERATURE_SMOOTH = 30

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        if kwargs.get('psk'):
            self.logger.warning('JK usually does not use a pairing PIN')
        self._buffer = bytearray()
        self._resp_table: Dict[int, Tuple[bytearray, float]] = {}
        self.num_cells = None
        self._callbacks: Dict[int, List[Callable[[bytes], None]]] = defaultdict(List)
        self.char_handle_notify = None
        self.char_handle_write = None

    def _buffer_crc_check(self):
        crc_comp = calc_crc(self._buffer[0:MIN_RESPONSE_SIZE - 1])
        crc_expected = self._buffer[MIN_RESPONSE_SIZE - 1]
        if crc_comp != crc_expected:
            self.logger.debug("crc check failed, %s != %s, %s", crc_comp, crc_expected, self._buffer)
        return crc_comp == crc_expected

    def _notification_handler(self, _sender, data):
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
                self.logger.debug("crc check failed, header at %d, discarding start of %s", idx, self._buffer)
                self._buffer = self._buffer[idx:]
                crc_ok = self._buffer_crc_check()

            if not crc_ok:
                self.logger.error("crc check failed, discarding buffer %s", self._buffer)
            else:
                self._decode_msg(bytearray(self._buffer))
            self._buffer.clear()

    def _decode_msg(self, buf: bytearray):
        resp_type = buf[4]
        self.logger.debug('got response %d (len%d)', resp_type, len(buf))
        self._resp_table[resp_type] = buf, time.time()
        self._fetch_futures.set_result(resp_type, self._buffer[:])
        callbacks = self._callbacks.get(resp_type, None)
        if callbacks:
            for cb in callbacks:
                cb(buf)

    async def connect(self, timeout=20):
        """
        Connecting JK with bluetooth appears to require a prior bluetooth scan and discovery, otherwise the connectiong fails with
        `[org.bluez.Error.Failed] Software caused connection abort`. Maybe the scan triggers some wake up?
        :param timeout:`
        :return:
        """

        try:
            await super().connect(timeout=6)
        except Exception as e:
            self.logger.info("normal connect failed (%s), connecting with scanner", str(e) or type(e))
            await self._connect_with_scanner(timeout=timeout)

        service = self.get_service(self.SERVICE_UUID)
        self.char_handle_write = self.find_char(self.CHAR_UUID, 'write', service=service)

        if self.char_handle_write and hasattr(self.char_handle_write,
                                              'handle') and self.char_handle_write.handle == 0x03:
            # from https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L197C17-L197C17
            self.char_handle_notify = self.find_char(0x05, 'notify')

        if not self.char_handle_notify:
            # there might be 2 chars with same uuid (weird?), one for notify/read and one for write
            # https://github.com/fl4p/batmon-ha/issues/83
            self.char_handle_notify = self.find_char(self.CHAR_UUID, 'notify')

        self.logger.debug('char_handle_notify=%s, char_handle_write=%s', self.char_handle_notify,
                          self.char_handle_write)

        await self.start_notify(self.char_handle_notify, self._notification_handler)

        await self._q(cmd=0x97, resp=0x03)  # device info
        await self._q(cmd=0x96, resp=(0x02, 0x01))  # device state (resp 0x01 & 0x02)
        # after these 2 commands the bms will continuously send 0x02-type messages

        buf, _ = self._resp_table[0x01]
        self.num_cells = buf[114]
        assert 0 < self.num_cells <= 24, "num_cells unexpected %s" % self.num_cells
        # self.capacity = int.from_bytes(buf[130:134], byteorder='little', signed=False) * 0.001

    async def disconnect(self):
        await self.client.stop_notify(self.char_handle_notify)
        await super().disconnect()

    async def _q(self, cmd, resp):
        await asyncio.sleep(.1)
        with await self._fetch_futures.acquire_timeout(resp, timeout=self.TIMEOUT / 2):
            frame = _jk_command(cmd, [])
            self.logger.debug("write %s", frame)
            await self.client.write_gatt_char(self.char_handle_write, data=frame)
            return await self._fetch_futures.wait_for(resp, self.TIMEOUT)

    async def _write(self, address, value):
        frame = _jk_command(address, value)
        self.logger.debug("write> %s", frame)
        await self.client.write_gatt_char(self.char_handle_write, data=frame)

    async def fetch_device_info(self):
        # https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jkabstractprotocol.py
        # https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L1152
        buf, _ = self._resp_table[0x03]
        psk = read_str(buf, 6 + 16 + 8 + 16 + 40 + 11)
        if psk:
            self.logger.info("PSK = '%s' (Note that anyone within BLE range can read this!)", psk)
        return DeviceInfo(mnf="JK",
                          model=read_str(buf, 6),
                          hw_version=read_str(buf, 6 + 16),
                          sw_version=read_str(buf, 6 + 16 + 8),
                          name=read_str(buf, 6 + 16 + 8 + 16),
                          sn=read_str(buf, 6 + 16 + 8 + 16 + 40),
                          )

    def _decode_sample(self, buf: bytearray, t_buf: float) -> BmsSample:
        buf_set, t_set = self._resp_table[0x01]

        is_new_11fw = buf[189] == 0x00 and buf[189 + 32] > 0
        offset = 0
        if is_new_11fw:
            offset = 32
            self.logger.debug('New 11.x firmware, offset=%s', offset)

        i16 = lambda i: int.from_bytes(buf[i:(i + 2)], byteorder='little', signed=True)
        u32 = lambda i: int.from_bytes(buf[i:(i + 4)], byteorder='little', signed=False)
        f32u = lambda i: u32(i) * 1e-3
        f32s = lambda i: int.from_bytes(buf[i:(i + 4)], byteorder='little', signed=True) * 1e-3

        temp = lambda x: float('nan') if x == -2000 else (x / 10)

        return BmsSample(
            voltage=f32u(118 + offset),
            current=-f32s(126 + offset),
            soc=buf[141 + offset],

            cycle_capacity=f32u(154 + offset),  # total charge TODO rename cycle charge
            capacity=f32u(146 + offset),  # computed capacity (starts at self.capacity, which is user-defined),
            charge=f32u(142 + offset),  # "remaining capacity"

            temperatures=[temp(i16(130 + offset)), temp(i16(132 + offset))],
            mos_temperature=i16((112 if is_new_11fw else 134) + offset) / 10,
            balance_current=i16(138 + offset) / 1000,

            # 146 charge_full (see above)
            num_cycles=u32(150 + offset),
            switches=dict(
                charge=bool(buf_set[118]),
                discharge=bool(buf_set[122]),
                balance=bool(buf_set[126]),
            ),
            #  #buf[166 + offset]),  charge FET state
            # buf[167 + offset]), discharge FET state
            uptime=float(u32(162 + offset)),  # seconds
            timestamp=t_buf,
        )

    async def fetch(self, wait=True) -> BmsSample:

        """
        Decode JK02
        references
        * https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L360
        * https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py
        """

        if wait:
            with await self._fetch_futures.acquire_timeout(0x02, timeout=self.TIMEOUT / 2):
                await self._fetch_futures.wait_for(0x02, self.TIMEOUT)

        if 0x01 not in self._resp_table:
            await self._q(cmd=0x96, resp=0x01)  # query settings

        buf, t_buf = self._resp_table[0x02]
        return self._decode_sample(buf, t_buf)

    async def subscribe(self, callback: Callable[[BmsSample], None]):
        self._callbacks[0x02].append(lambda buf: callback(self._decode_sample(buf)))

    async def fetch_voltages(self):
        """
        :return: list of cell voltages in mV
        """
        if self.num_cells is None:
            raise Exception("num_cells not set")
        buf, t_buf = self._resp_table[0x02]
        voltages = [int.from_bytes(buf[(6 + i * 2):(6 + i * 2 + 2)], byteorder='little') for i in
                    range(self.num_cells)]
        return voltages

    async def set_switch(self, switch: str, state: bool):
        # from https://github.com/syssi/esphome-jk-bms/blob/4079c22eaa40786ffa0cabd45d0d98326a1fdd29/components/jk_bms_ble/switch/__init__.py
        addresses = dict(
            charge=0x1D,
            discharge=0x1E,
            balance=0x1F
        )
        await self._write(addresses[switch], [0x1 if state else 0x0, 0, 0, 0])
        await asyncio.sleep(.2) # wait a bit before triggering settings fetch
        self._resp_table.pop(0x01, None)  # invalidate settings frame which stores switch states
        #await asyncio.sleep(0.2)  # not sure if this is needed


    def debug_data(self):
        return dict(resp=self._resp_table, char_w=self.char_handle_write, char_r=self.char_handle_notify)


async def main():
    # _jk_command(0x96)

    # await bmslib.bt.bt_discovery(logger=get_logger())
    mac_address = 'F21958DF-E949-4D43-B12B-0020365C428A'  # caravan
    # mac_address = '46A9A7A1-D6C6-59C5-52D0-79EC8C77F4D2'  # bat100ah
    mac_address = 'BB92A45B-ABA1-2EA8-1BD3-DA140771C79D'  # caravan (intel)

    bms = JKBt(mac_address, name='jk', verbose_log=False)
    async with bms:
        while True:
            s = await bms.fetch(wait=True)
            # print(s, 'I_bal=', s.balance_current, await bms.fetch_voltages())
            print(s.switches)

            b = not s.switches.get("charge")

            await bms.set_switch("charge", b)

            s = await bms.fetch()
            print(s.switches)

            if s.switches.get("charge") != b:
                print('error', s)

            # new_state = not s.switches['charge']
            # await bms.set_switch('charge', new_state)
            # await bms._q(cmd=0x96, resp= 0x01)
            # print('set charge', new_state)
            # await asyncio.sleep(4)
            # s = await bms.fetch(wait=True)
            # print(s)


if __name__ == '__main__':
    asyncio.run(main())
