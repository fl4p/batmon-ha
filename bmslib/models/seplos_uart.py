"""Seplos BMS V2 (BMS 1.0 / 2.0 hardware, the 48 V rack packs) over RS485 (wired).

The Seplos V2 protocol is the ASCII-hex ``~ VER ADR CID1 CID2 LENGTH INFO CHKSUM \\r``
framing shared with PACE ("paceic"), with VER = 0x20 and CID1 = 0x46. The frame
builder and parser are therefore reused from ``bmslib.models.pace``; only the INFO
layouts differ:

  CID2 0x42 telemetry (analog), INFO after the return code:
    data_flag u8 | pack u8 | n_cells u8 | cell mV u16 x n | n_temps u8 |
    temp 0.1 K u16 x n | current 0.01 A s16 | voltage 0.01 V u16 |
    remaining 0.01 Ah u16 | user-defined count u8 (10) | full capacity 0.01 Ah u16 |
    SOC 0.1 % u16 | rated capacity 0.01 Ah u16 | cycles u16 | SOH 0.1 % u16 |
    port voltage 0.01 V u16

  CID2 0x44 telesignalization (alarms/switches), INFO after the return code:
    data_flag u8 | pack u8 | n_cells u8 | cell alarm u8 x n | n_temps u8 |
    temp alarm u8 x n | charge current alarm u8 | pack voltage alarm u8 |
    discharge current alarm u8 | n_events u8 | event bytes ...
    event 1 voltage, 2 temperature, 3 other, 4 current, 5 SOC/misc,
    6 switch status (bit0 discharge MOSFET on, bit1 charge MOSFET on)

Sources: Seplos "BMS communication protocol V2.0" PDF; dbus-serialbattery
``bms/seplos.py`` (offsets 96/100/104/110/114/122 for a 16S 6T pack, alarm bytes
30-35); aiobmsble ``seplos_v2_bms.py`` (same fields over BLE, cross-checked).

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    seplos_uart          # or seplos_uart:1 for pack address 1
    alias:   any human-readable name

Link: 19200 8N1 on the RS485 port (9600 on the RS232 console port; set
``BAUDRATE`` via ``type: seplos_uart:<addr>:9600`` if you use that one).
Seplos V3 (BMS 3.0, Modbus RTU) is a different protocol and not covered here.
Untested on hardware.
"""
import math
from typing import List, Optional

from bmslib.bms import BmsSample
from bmslib.bt import BtBms
from bmslib.models.pace import SOI, EOI, build_frame, parse_frame, _Cursor, CID2_OK

VER = 0x20
CID1 = 0x46
CID2_TELEMETRY = 0x42
CID2_TELESIGNAL = 0x44
TEMP_ZERO_TENTHS_K = 2731


def decode_telemetry(info: bytes) -> dict:
    c = _Cursor(info)
    c.u8()  # data flag
    pack = c.u8()
    n_cells = c.u8()
    if not 1 <= n_cells <= 32:
        raise ValueError(f"seplos telemetry: implausible cell count {n_cells}")
    cell_mv = [c.u16() for _ in range(n_cells)]
    n_temps = c.u8()
    if not 0 <= n_temps <= 16:
        raise ValueError(f"seplos telemetry: implausible temp count {n_temps}")
    temps = [(c.u16() - TEMP_ZERO_TENTHS_K) / 10 for _ in range(n_temps)]
    current = c.s16() / 100
    voltage = c.u16() / 100
    remaining = c.u16() / 100
    n_user = c.u8()
    capacity = c.u16() / 100
    soc = c.u16() / 10
    rated = c.u16() / 100
    cycles = c.u16()
    soh = c.u16() / 10 if n_user >= 8 and not c.at_end() else math.nan
    port_voltage = c.u16() / 100 if n_user >= 9 and not c.at_end() else math.nan
    if sum(cell_mv) == 0:
        raise ValueError("seplos telemetry: all cell voltages zero")
    return dict(pack=pack, cell_mv=cell_mv, temps=temps, current=current, voltage=voltage,
                remaining=remaining, capacity=capacity, soc=soc, rated=rated, cycles=cycles,
                soh=soh, port_voltage=port_voltage)


def decode_telesignal(info: bytes) -> dict:
    c = _Cursor(info)
    c.u8()
    pack = c.u8()
    n_cells = c.u8()
    if not 1 <= n_cells <= 32:
        raise ValueError(f"seplos telesignal: implausible cell count {n_cells}")
    cell_alarm = [c.u8() for _ in range(n_cells)]
    n_temps = c.u8()
    if not 0 <= n_temps <= 16:
        raise ValueError(f"seplos telesignal: implausible temp count {n_temps}")
    temp_alarm = [c.u8() for _ in range(n_temps)]
    chg_current_alarm = c.u8()
    voltage_alarm = c.u8()
    dsg_current_alarm = c.u8()
    n_events = c.u8()
    events = [c.u8() for _ in range(n_events)]
    if len(events) < 6:
        raise ValueError(f"seplos telesignal: only {len(events)} event bytes")
    switch = events[5]
    problem_code = int.from_bytes(bytes(events[:5]), 'big')
    warnings = any(v not in (0x00, 0x01) for v in cell_alarm + temp_alarm) or \
        any(v not in (0x00, 0x01) for v in (chg_current_alarm, voltage_alarm, dsg_current_alarm))
    return dict(pack=pack, problem_code=problem_code, problem=bool(problem_code) or warnings,
                discharge_mosfet=bool(switch & 0x01), charge_mosfet=bool(switch & 0x02))


class SeplosUart(BtBms):
    BAUDRATE = 19200
    SERIAL_KWARGS = dict(eol=bytes([EOI]), timeout=2)
    TIMEOUT = 10
    _KEY = 'seplos'

    def __init__(self, address, **kwargs):
        spec = kwargs.pop('type_spec', None)
        parts = [p for p in (spec or '').split(':') if p]
        self.pack_addr = int(parts[0], 0) if parts else 0
        if len(parts) > 1:
            self.BAUDRATE = int(parts[1])
        if not 0 <= self.pack_addr <= 0xFF:
            raise ValueError("seplos pack address must be 0..255, got %r" % (self.pack_addr,))
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_cells: List[int] = []
        self._last_temps: List[float] = []

    def _notification_handler(self, sender, data):
        self._buffer += bytes(data)
        while True:
            start = self._buffer.find(SOI)
            if start < 0:
                self._buffer.clear()
                return
            if start:
                del self._buffer[:start]
            end = self._buffer.find(EOI)
            if end < 0:
                return
            frame = bytes(self._buffer[:end + 1])
            del self._buffer[:end + 1]
            try:
                fields = parse_frame(frame)
            except ValueError as exc:
                self.logger.warning("%s discarding invalid seplos frame: %s", self.name, exc)
                continue
            if fields['adr'] != self.pack_addr:
                continue  # another pack on the bus
            self._fetch_futures.set_result(self._KEY, fields)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("seplos-uart-%d" % self.pack_addr, "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await super().disconnect()

    async def _read(self, cid2: int) -> dict:
        req = build_frame(VER, self.pack_addr, CID1, cid2, b'%02X' % self.pack_addr)

        async def exchange():
            with self._fetch_futures.acquire(self._KEY):
                await self.client.write_gatt_char(self.UUID_TX, data=req)
                return await self._fetch_futures.wait_for(self._KEY, self.TIMEOUT)

        lock = getattr(self.client, 'bus_lock', None)
        fields = await exchange() if lock is None else await _locked(lock, exchange)
        if fields['cid2'] != CID2_OK:
            raise ValueError(f"seplos returned error code 0x{fields['cid2']:02X} for request 0x{cid2:02X}")
        return fields

    async def fetch(self) -> BmsSample:
        t = decode_telemetry((await self._read(CID2_TELEMETRY))['info'])
        s: Optional[dict] = None
        try:
            s = decode_telesignal((await self._read(CID2_TELESIGNAL))['info'])
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("%s seplos telesignal read failed, using telemetry only: %s", self.name, exc)

        temps = t['temps']
        # 4 cell probes, then environment, then power stage (dbus-serialbattery: index 5 = MOSFET)
        mos_temperature = temps[5] if len(temps) > 5 else math.nan
        temperatures = temps[:5] if len(temps) > 5 else temps
        self._last_cells = t['cell_mv']
        self._last_temps = temperatures
        return BmsSample(
            voltage=t['voltage'],
            current=-t['current'],  # Seplos: positive = charging
            charge=t['remaining'],
            capacity=t['rated'] if t['rated'] > 0 else t['capacity'],
            aged_capacity=t['capacity'],
            soc=t['soc'],
            soh=t['soh'],
            num_cycles=t['cycles'],
            temperatures=temperatures,
            mos_temperature=mos_temperature,
            switches=(dict(charge=s['charge_mosfet'], discharge=s['discharge_mosfet']) if s else None),
            problem=s['problem'] if s else None,
            problem_code=s['problem_code'] if s else None,
        )

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps


async def _locked(lock, coro_fn):
    async with lock():
        return await coro_fn()
