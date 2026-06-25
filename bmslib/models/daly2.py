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
MODBUS_WRITE_SINGLE = 0x06

# Registers that toggle the MOSFETs (Modbus write-single, fct 0x06, value
# 1=on / 0=off). Confirmed against an HCI snoop of the official Daly app (#356):
# toggling charge wrote `D2 06 00 A5 00 01/00`, discharge wrote `D2 06 00 A6
# 00 01/00`. These are the "charging MOS switch" (0xA5) / "discharge MOS switch"
# (0xA6) registers from the Daly Modbus spec.
#
# NB: the host commands 0x000C/0x000D (an earlier guess from the spec's "enable
# battery discharge" example) echo a valid write but do NOT actuate the physical
# MOSFET on this firmware. Despite the official app prompting for the parameter
# password (123456), the snoop shows it is NEVER written to the BMS before the
# switch write - the app only *reads* it (reg 0xC9) to validate the user's input
# locally. So no unlock sequence is needed; the 0xA5/0xA6 write stands alone.
SWITCH_REGISTERS = dict(charge=0x00A5, discharge=0x00A6)


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


def _frame(body: bytes) -> bytes:
    """Append the little-endian Modbus CRC to a frame body."""
    crc = _modbus_crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def _read_request(start: int, count: int) -> bytes:
    """Build a Modbus read-holding-registers request frame with CRC."""
    return _frame(bytes([MODBUS_ADDRESS, MODBUS_READ_HOLDING,
                         (start >> 8) & 0xFF, start & 0xFF,
                         (count >> 8) & 0xFF, count & 0xFF]))


def _write_request(reg: int, value: int) -> bytes:
    """Build a Modbus write-single-register request frame with CRC."""
    return _frame(bytes([MODBUS_ADDRESS, MODBUS_WRITE_SINGLE,
                         (reg >> 8) & 0xFF, reg & 0xFF,
                         (value >> 8) & 0xFF, value & 0xFF]))


class Daly2Bt(BtBms):
    TIMEOUT = 8
    SET_SWITCH_TIMEOUT = 4

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
        self._last_block = None
        self.UUID_RX = None
        self.UUID_TX = None

    def _notification_handler(self, _sender, data):
        self.logger.debug("ble data frame %s", data)
        self._buffer += data

        if len(self._buffer) < 3:
            return

        func = self._buffer[1]
        if func == MODBUS_READ_HOLDING:
            # [addr][0x03][bytecount][...data...][crc_lo][crc_hi]
            expected = 3 + self._buffer[2] + 2
        elif func == MODBUS_WRITE_SINGLE:
            # echo: [addr][0x06][reg_hi][reg_lo][val_hi][val_lo][crc_lo][crc_hi]
            expected = 8
        elif func & 0x80:
            # exception: [addr][func|0x80][exc_code][crc_lo][crc_hi]
            expected = 5
        else:
            self.logger.warning("daly2 unexpected func 0x%02x: %s", func, self._buffer.hex())
            self._buffer.clear()
            return

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

    async def _q(self, request: bytes, timeout=None):
        func = request[1]
        self._buffer.clear()
        with self._fetch_futures.acquire(func):
            self.logger.debug("daly2 send: %s", request.hex())
            await self.client.write_gatt_char(self.UUID_TX, request, response=False)
            return await self._fetch_futures.wait_for(func, timeout or self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        # binary reading
        #  https://github.com/roccotsi2/esp32-smart-bms-simulation

        # read 0x3E (62) holding registers starting at 0 -> 124-byte payload
        buf = await self._q(_read_request(0x0000, 0x003E))
        buf = buf[3:]
        self._last_block = buf  # reused by fetch_voltages() within the same cycle

        #num_cell = int.from_bytes(buf[98:100], 'big')
        num_temp = int.from_bytes(buf[100:102], 'big')

        sample = BmsSample(
            voltage=int.from_bytes(buf[80:82], byteorder='big') / 10,
            current=(int.from_bytes(buf[82:84], byteorder='big') - 30000) / 10,
            soc=int.from_bytes(buf[84:86], byteorder='big') / 10,

            charge=int.from_bytes(buf[96:98], byteorder='big') / 10,
            #capacity=int.from_bytes(buf[6:8], byteorder='big', signed=True) / 100,

            num_cycles=int.from_bytes(buf[102:104], byteorder='big'),

            temperatures=[(int.from_bytes(buf[64 + i * 2:i * 2 + 66], 'big') - 40) for i in range(num_temp)],

            # Separate charge/discharge MOSFET state registers (matches aiobmsble
            # daly_bms). The old single mos_byte at buf[20] was a5-protocol logic.
            switches=dict(
                charge=bool(int.from_bytes(buf[106:108], byteorder='big')),
                discharge=bool(int.from_bytes(buf[108:110], byteorder='big')),
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

    async def fetch_voltages(self, num_cells=0):
        # Cell voltages (mV, big-endian) sit at the start of the 0x3E block,
        # cell_count at register offset 98. Reuse the block read by fetch() in
        # the same sampling cycle; fall back to a fresh read if called alone.
        buf = self._last_block
        if buf is None:
            buf = (await self._q(_read_request(0x0000, 0x003E)))[3:]
        if not num_cells:
            num_cells = int.from_bytes(buf[98:100], 'big')
        num_cells = min(num_cells, 48)
        return [int.from_bytes(buf[i * 2:i * 2 + 2], 'big') for i in range(num_cells)]

    async def set_switch(self, switch: str, state: bool):
        reg = SWITCH_REGISTERS.get(switch)
        if reg is None:
            raise ValueError("unknown switch %s" % switch)

        req = _write_request(reg, 1 if state else 0)
        self.logger.info("daly2 set %s mosfet -> %s: %s", switch, state, req.hex())
        # A correct write echoes the request frame (func 0x06). If the register
        # is wrong the BMS stays silent, so don't let the missing echo turn into
        # a fatal 8s-blocking traceback in the mqtt action queue.
        try:
            echo = await self._q(req, timeout=self.SET_SWITCH_TIMEOUT)
            self.logger.info("daly2 %s mosfet write echoed: %s", switch, echo.hex())
            if self._switches is not None:
                self._switches[switch] = state
        except TimeoutError:
            self.logger.warning(
                "daly2 %s mosfet write to reg 0x%04x got no echo - register may be "
                "wrong for this firmware or the command was rejected", switch, reg)


