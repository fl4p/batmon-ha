"""
Other Daly protocol

References
- https://github.com/roccotsi2/esp32-smart-bms-simulation

-  https://github.com/tomatensaus/python-daly-bms

"""

from bmslib.bms import BmsSample
from bmslib.bt import BtBms, enumerate_services

# Daly Modbus slave address used on BLE (host->BMS). 0xD2 is what the official
# app and ESPHome's daly_bms_ble send.
MODBUS_ADDRESS = 0xD2
MODBUS_READ_HOLDING = 0x03


def _modbus_crc16(data: bytes) -> int:
    """Standard Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF). Appended little-endian."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _read_request(start: int, count: int) -> bytes:
    """Build a Modbus read-holding-registers request frame with CRC."""
    body = bytes([MODBUS_ADDRESS, MODBUS_READ_HOLDING,
                  (start >> 8) & 0xFF, start & 0xFF,
                  (count >> 8) & 0xFF, count & 0xFF])
    crc = _modbus_crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


class Daly2Bt(BtBms):
    TIMEOUT = 8

    # rx (notify), tx (write). Older firmware exposes the fff0 service; newer
    # DL/JHB firmware (#356) returns org.bluez.Error.NotPermitted on fff1 and
    # exposes service 0000ff00 with ff01=notify / ff02=write instead.
    CHARACTERISTIC_UUIDS = [
        ('0000fff1-0000-1000-8000-00805f9b34fb', '0000fff2-0000-1000-8000-00805f9b34fb'),
        ('0000ff01-0000-1000-8000-00805f9b34fb', '0000ff02-0000-1000-8000-00805f9b34fb'),
    ]

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._switches = None
        self.UUID_RX = None
        self.UUID_TX = None

    def _notification_handler(self, _sender, data):
        self.logger.debug("ble data frame %s", data)
        self._buffer += data

        # Modbus response: [addr][func][bytecount][...data...][crc_lo][crc_hi]
        if len(self._buffer) < 3:
            return
        expected = 3 + self._buffer[2] + 2
        if len(self._buffer) < expected:
            return

        frame = bytes(self._buffer[:expected])
        self._buffer.clear()

        func = frame[1]
        crc_calc = _modbus_crc16(frame[:-2])
        crc_recv = frame[-2] | (frame[-1] << 8)
        if crc_calc != crc_recv:
            self.logger.warning("daly2 crc fail (calc %04x != recv %04x): %s",
                                crc_calc, crc_recv, frame.hex())
            return

        self._fetch_futures.set_result(func, frame)

    async def connect(self, **kwargs):
        await super().connect(**kwargs)

        for rx, tx in self.CHARACTERISTIC_UUIDS:
            try:
                await self.client.start_notify(rx, self._notification_handler)
                self.UUID_RX = rx
                self.UUID_TX = tx
                self.logger.debug("found rx uuid to be working: %s (tx %s)", rx, tx)
                break
            except Exception as e:
                self.logger.warning("tried rx/tx uuids %s/%s: %s", rx, tx, e)
                continue

        if not self.UUID_RX:
            await enumerate_services(self.client, self.logger)
            raise Exception("Notify characteristic (rx) not found")

    async def disconnect(self):
        if self.UUID_RX:
            await self.client.stop_notify(self.UUID_RX)
        self._fetch_futures.clear()
        await super().disconnect()

    async def _q(self, request: bytes):
        func = request[1]
        self._buffer.clear()
        with self._fetch_futures.acquire(func):
            self.logger.debug("daly2 send: %s", request.hex())
            await self.client.write_gatt_char(self.UUID_TX, request, response=False)
            return await self._fetch_futures.wait_for(func, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        # binary reading
        #  https://github.com/roccotsi2/esp32-smart-bms-simulation

        # read 0x3E (62) holding registers starting at 0 -> 124-byte payload
        buf = await self._q(_read_request(0x0000, 0x003E))
        buf = buf[3:]

        #num_cell = int.from_bytes(buf[21:22], 'big')
        num_temp = int.from_bytes(buf[100:102], 'big')

        mos_byte = int.from_bytes(buf[20:21], 'big')

        sample = BmsSample(
            voltage=int.from_bytes(buf[80:82], byteorder='big') / 10,
            current=(int.from_bytes(buf[82:84], byteorder='big') - 30000) / 10,
            soc=int.from_bytes(buf[84:86], byteorder='big') / 10,

            charge=int.from_bytes(buf[96:98], byteorder='big') / 10,
            #capacity=int.from_bytes(buf[6:8], byteorder='big', signed=True) / 100,

            num_cycles=int.from_bytes(buf[102:104], byteorder='big'),

            temperatures=[(int.from_bytes(buf[64 + i * 2:i * 2 + 66], 'big') - 40) for i in range(num_temp)],

            switches=dict(
                discharge=mos_byte == 2 or mos_byte == 3,
                charge=mos_byte == 1 or mos_byte == 3,
            ),

            # Alarm bitmask at byte 116 (8 bytes, big-endian) per aiobmsble's
            # daly_bms decode. Daly v2 / Modbus protocol response covers cell
            # OV/UV, pack OV/UV, charge/discharge OC, OT/UT, balance, MOSFET
            # faults, etc.
            problem_code=int.from_bytes(buf[116:124], byteorder='big', signed=False),

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


