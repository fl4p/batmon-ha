"""Renogy smart lithium batteries (RBT100LFP12-BT, RBT200LFP12S, RBT50LFP48S, ...)
over RS485 (wired), Modbus RTU function 0x03.

Register map (holding registers, big-endian):
    5000 (0x1388)   cell count
    5001..5016      cell voltages, 0.1 V
    5017 (0x1399)   temperature sensor count
    5018..5033      cell temperatures, 0.1 degC
    5042 (0x13B2)   current, s16, 0.01 A (positive = charging)
    5043            pack voltage, 0.1 V
    5044-5045       remaining charge, u32, mAh
    5046-5047       full capacity, u32, mAh
    5048            cycle count
    5100 (0x13EC)   7 registers of alarm bits, then 5107 status flags
                    (byte 0 bit1 charge MOSFET on, bit2 discharge MOSFET on;
                     byte 1 bit5 heater on)
    5104 (0x13F0)   28 registers device info (serial 5110, model 5122, fw 5130)

Sources: aiobmsble ``renogy_bms.py`` (same registers through the BT-2 dongle,
cross-checked), dbus-serialbattery ``bms/renogy.py``, cyrils/renogy-bt.

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    renogy_uart          # or renogy_uart:0xF7 for slave id 0xF7
    alias:   any human-readable name

Slave id: 0x30 (48) on most packs, 0xF7 on the RBT100LFP12SH-G1 per
dbus-serialbattery. 9600 8N1. Untested on hardware.
"""
import math
from typing import List, Optional

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms
from bmslib.modbus_rtu import build_read, extract_read_frame, ModbusError, u16_be, u32_be, FC_READ_HOLDING

REG_CELLS = 0x1388      # 5000, read 0x22 registers: count, 16 cells, temp count, 16 temps
REG_SOC = 0x13B2        # 5042, read 7: current, voltage, remaining(2), capacity(2), cycles
REG_ALARM = 0x13EC      # 5100, read 8
REG_INFO = 0x13F0       # 5104, read 0x1C


def decode_soc_block(data: bytes) -> dict:
    if len(data) < 14:
        raise ValueError(f"renogy soc block too short: {len(data)} bytes")
    return dict(
        current=u16_be(data, 0, signed=True) / 100,
        voltage=u16_be(data, 2) / 10,
        remaining=u32_be(data, 4) / 1000,
        capacity=u32_be(data, 8) / 1000,
        cycles=u16_be(data, 12),
    )


def decode_cell_block(data: bytes) -> dict:
    if len(data) < 68:
        raise ValueError(f"renogy cell block too short: {len(data)} bytes")
    n_cells = u16_be(data, 0)
    n_temps = u16_be(data, 34)
    if not 1 <= n_cells <= 16:
        raise ValueError(f"renogy: implausible cell count {n_cells}")
    if not 0 <= n_temps <= 16:
        raise ValueError(f"renogy: implausible temperature sensor count {n_temps}")
    cells = [u16_be(data, 2 + 2 * i) * 100 for i in range(n_cells)]  # 0.1 V -> mV
    temps = [u16_be(data, 36 + 2 * i, signed=True) / 10 for i in range(n_temps)]
    return dict(cell_mv=cells, temps=temps)


def decode_alarm_block(data: bytes) -> dict:
    if len(data) < 16:
        raise ValueError(f"renogy alarm block too short: {len(data)} bytes")
    # 7 alarm registers; aiobmsble masks bits 1-3 of the last byte (status noise)
    problem_code = int.from_bytes(data[0:14], 'big') & ~0xE
    # aiobmsble reads the flags at full-frame offsets 16/17, i.e. payload 13/14:
    # the low byte of the 7th register and the high byte of the 8th.
    return dict(
        problem_code=problem_code,
        charge_mosfet=bool(data[13] & 0x02),
        discharge_mosfet=bool(data[13] & 0x04),
        heater=bool(data[14] & 0x20),
    )


class RenogyUart(BtBms):
    BAUDRATE = 9600
    SERIAL_KWARGS = dict(eol=None, timeout=1)
    TIMEOUT = 6
    _KEY = 'renogy'

    def __init__(self, address, **kwargs):
        spec = kwargs.pop('type_spec', None)
        self.slave = int(spec, 0) if spec else 0x30
        if not 1 <= self.slave <= 0xF7:
            raise ValueError("renogy slave id must be 1..247, got %r" % (self.slave,))
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_cells: List[int] = []
        self._last_temps: List[float] = []

    def _notification_handler(self, sender, data):
        self._buffer += bytes(data)
        while True:
            try:
                res = extract_read_frame(self._buffer, slave=self.slave)
            except ModbusError as exc:
                self.logger.warning("%s %s", self.name, exc)
                self._fetch_futures.set_result(self._KEY, exc)
                continue
            if res is None:
                return
            _slave, _fc, payload = res
            self._fetch_futures.set_result(self._KEY, payload)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("renogy-uart-%d" % self.slave, "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await super().disconnect()

    async def _read(self, reg: int, count: int) -> bytes:
        req = build_read(self.slave, reg, count, FC_READ_HOLDING)

        async def exchange():
            # Start every exchange clean: an echoed request or a stale partial
            # frame must not be able to shift where the reply is looked for.
            self._buffer.clear()
            with self._fetch_futures.acquire(self._KEY):
                await self.client.write_gatt_char(self.UUID_TX, data=req)
                res = await self._fetch_futures.wait_for(self._KEY, self.TIMEOUT)
            if isinstance(res, Exception):
                raise res
            if len(res) != count * 2:
                raise ValueError(f"renogy reg 0x{reg:04X}: expected {count * 2} bytes, got {len(res)}")
            return res

        lock = getattr(self.client, 'bus_lock', None)
        if lock is None:
            return await exchange()
        async with lock():
            return await exchange()

    async def fetch(self) -> BmsSample:
        soc = decode_soc_block(await self._read(REG_SOC, 7))
        cells = decode_cell_block(await self._read(REG_CELLS, 0x22))
        alarm: Optional[dict] = None
        try:
            alarm = decode_alarm_block(await self._read(REG_ALARM, 8))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("%s renogy alarm read failed: %s", self.name, exc)

        self._last_cells = cells['cell_mv']
        self._last_temps = cells['temps']
        capacity = soc['capacity']
        return BmsSample(
            voltage=soc['voltage'],
            current=-soc['current'],  # Renogy: positive = charging
            charge=soc['remaining'],
            capacity=capacity,
            soc=(soc['remaining'] / capacity * 100) if capacity > 0 else math.nan,
            num_cycles=soc['cycles'],
            temperatures=cells['temps'] or None,
            switches=(dict(charge=alarm['charge_mosfet'], discharge=alarm['discharge_mosfet']) if alarm else None),
            problem=bool(alarm['problem_code']) if alarm else None,
            problem_code=alarm['problem_code'] if alarm else None,
        )

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps

    async def fetch_device_info(self) -> DeviceInfo:
        d = await self._read(REG_INFO, 0x1C)

        def s(off, n):
            return d[off:off + n].split(b'\x00', 1)[0].decode('ascii', errors='replace').strip()

        # offsets from aiobmsble (frame offsets 15/39/55 minus the 3-byte header)
        return DeviceInfo(mnf='Renogy', model=s(36, 16), hw_version=None, sw_version=s(52, 4),
                          name=s(36, 16), sn=s(12, 16))

    async def set_switch(self, switch: str, state: bool):
        raise NotImplementedError("Renogy MOSFETs cannot be switched over RS485")
