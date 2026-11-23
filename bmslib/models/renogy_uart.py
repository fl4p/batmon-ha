"""Renogy smart lithium batteries (RBT100LFP12-BT, RBT200LFP12S, RBT50LFP48S, ...)
over RS485 (wired), Modbus RTU function 0x03.

Register map (holding registers, big-endian):
    5000 (0x1388)   cell count
    5001..5016      cell voltages, 0.1 V
    5017 (0x1399)   temperature sensor count
    5018..5033      cell temperatures, 0.1 degC
    5042 (0x13B2)   current, s16, 0.01 A, 0.1 A on some models (positive = charging)
    5043            pack voltage, 0.1 V
    5044-5045       remaining charge, u32, mAh
    5046-5047       full capacity, u32, mAh
    5048            cycle count
    5100 (0x13EC)   7 registers of alarm bits, then 5107 warning bits and status flags
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
from typing import Optional

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.uart.wired import WiredBms, bus_locked
from bmslib.uart.modbus_rtu import build_read, extract_read_frame, ModbusError, u16_be, u32_be, FC_READ_HOLDING

REG_CELLS = 0x1388      # 5000, read 0x22 registers: count, 16 cells, temp count, 16 temps
REG_SOC = 0x13B2        # 5042, read 7: current, voltage, remaining(2), capacity(2), cycles
REG_ALARM = 0x13EC      # 5100, read 8
REG_INFO = 0x13F0       # 5104, read 0x1C
REG_MODEL = 0x1402      # 5122, read 8: model (SKU), ASCII


# Models whose current register counts 0.1 A instead of 0.01 A (Renogy app 1.10.85, v4/a.f, substring match on the
# model at 5122; doc/dev/RENOGY_PROTOCOL.md)
CURRENT_DECI_AMP_MODELS = ('RBT12100LFP-BT', 'RBT12200LFP-BT', 'RBT12104LFP-SSL', 'RBT-L12100', 'RBT-L12200',
                           'RBT-L12314', 'RBT12200LFP-B', 'RBT48105LFP-GC', 'RBT-L12150U-B1')
# 5107 bits the app shows as warnings: cell/pack undervoltage, charge/discharge low/high temperature. Bit 13 is the
# heater state.
WARNING_MASK_5107 = 0x00F5


def current_divider(model: str) -> int:
    return 10 if any(m in (model or '') for m in CURRENT_DECI_AMP_MODELS) else 100


def problem_code_5100_5107(data: bytes) -> int:
    """5100-5106 (MOSFET state bits 1-3 of 5106 masked, as aiobmsble does), then the 5107 warnings in the low 16 bits."""
    return (int.from_bytes(data[0:14], 'big') & ~0xE) << 16 | (int.from_bytes(data[14:16], 'big') & WARNING_MASK_5107)


def decode_soc_block(data: bytes, divider: int = 100) -> dict:
    if len(data) < 14:
        raise ValueError(f"renogy soc block too short: {len(data)} bytes")
    return dict(
        current=u16_be(data, 0, signed=True) / divider,
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
    problem_code = problem_code_5100_5107(data)
    # aiobmsble reads the flags at full-frame offsets 16/17, i.e. payload 13/14:
    # the low byte of the 7th register and the high byte of the 8th.
    return dict(
        problem_code=problem_code,
        charge_mosfet=bool(data[13] & 0x02),
        discharge_mosfet=bool(data[13] & 0x04),
        heater=bool(data[14] & 0x20),
    )


class RenogyUart(WiredBms):
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

    def _char_name(self) -> str:
        return "renogy-uart-%d" % self.slave

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

        return await bus_locked(self.client, exchange)

    async def _current_divider(self) -> int:
        if getattr(self, '_divider', None) is None:
            try:
                raw = await self._read(REG_MODEL, 8)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("%s renogy model read failed (%s), current in 0.01 A until it works", self.name, exc)
                return 100
            model = raw.split(b'\x00', 1)[0].decode('ascii', errors='replace').strip()
            self._divider = current_divider(model)
            self.logger.info("%s renogy model %r: current in %s A", self.name, model, 1 / self._divider)
        return self._divider

    async def fetch(self) -> BmsSample:
        soc = decode_soc_block(await self._read(REG_SOC, 7), await self._current_divider())
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

    async def fetch_device_info(self) -> DeviceInfo:
        d = await self._read(REG_INFO, 0x1C)

        def s(off, n):
            return d[off:off + n].split(b'\x00', 1)[0].decode('ascii', errors='replace').strip()

        # offsets from aiobmsble (frame offsets 15/39/55 minus the 3-byte header)
        return DeviceInfo(mnf='Renogy', model=s(36, 16), hw_version=None, sw_version=s(52, 4),
                          name=s(36, 16), sn=s(12, 16))

    async def set_switch(self, switch: str, state: bool):
        raise NotImplementedError("Renogy MOSFETs cannot be switched over RS485")
