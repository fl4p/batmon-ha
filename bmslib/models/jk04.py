"""
JK04: BLE protocol of the old JK (Jikong) active balancers, `type: jk04`.

Devices: balancer-only units without MOSFETs or a current shunt, e.g. JK-B2A16S hw 3.0 sw 3.3.0,
JK-B2A24S hw 3.0 sw 3.2.3, JK-B5A24S hw 8.X sw 8.0.3M. The model name has no `P` (protection current)
suffix, unlike the JK BMSes (JK-B2A24S15P ...). There is no pack current, SoC or temperature on the wire.

Transport and framing are JK02's (models/jikong.py): service FFE0, char FFE1, commands `AA 55 90 EB <reg>
<len> <13 B payload> <sum8>`, 300 B responses `55 AA EB 90 <type> <counter> ... <sum8>`. 0x97 asks for the
device info (0x03 frame, same layout as JK02), 0x96 for the settings (0x01) after which the device
streams 0x02 cell info frames. Unlike JK02 every value is an IEEE-754 float32 (little endian).

0x02 cell info (offset, length, content):
    6    24x4  cell voltage 1..24, V (0 for unused cells)
  102    25x4  cell (wire) resistance, Ohm
  202     4    average cell voltage, V (the BMS's own, not exactly the mean of the cells)
  206     4    delta cell voltage, V
  214     4    0xFFFF for 16 cells, 0xFFFFFF for 24 cells (cell mask?), not decoded
  218     1    highest cell, 0-based (mpp-solar)
  219     1    lowest cell, 0-based (mpp-solar)
  220     1    balancer state: 0 idle, 1 charging balancer, 2 discharging balancer
  222     4    balancing current, A, signed
  233     4    unknown float32
  286     4    uptime, s (uint32)
  294     4    unknown float32
  299     1    sum8 CRC

0x01 settings:
   34     1    cell count (register 0x1C, 2 B)
   38     4    power off voltage, V (register 0x20)
   74     4    voltage calibration, ADC Vref (register 0x44)
   98     4    balancing start voltage, V (register 0x58)
  102     4    unknown, 3.4 on both known devices
  106     4    balance trigger delta voltage, V (register 0x64)
  110     4    max balance current, A (register 0x68)
  114     1    balancer switch (register 0x6C, 1 B)

`type: jk` cannot tell a JK04 device from JK02 before its first cell info frame, and the device info
gives no reliable sign (hw/sw 8.x exists in both families). It reports a JK04 cell frame as implausible
with a hint to set `type: jk04` (looks_like_jk04_status) instead of decoding it.

Not tested on hardware. Offsets and fixture frames (bmslib/test/data/jk04_fixtures.py, see SOURCES.md):
* syssi/esphome-jk-bms (Apache-2.0), components/jk_bms_ble/jk_bms_ble.cpp decode_jk04_cell_info_,
  decode_jk04_settings_, decode_device_info_; tests/components/jk_bms_ble/frames_jk04*.h
  https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp
  https://github.com/syssi/esphome-jk-bms/issues/213 (JK-B5A24S frames + app write capture)
  https://github.com/syssi/esphome-jk-bms/discussions/48 (JK-B2A16S frames, balancer switch 0x6C)
* jblance/mpp-solar (MIT), mppsolar/protocols/jk04.py, tests/integration/test_protocol_jk04.py
  https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk04.py
"""
import asyncio
import math
import struct
import time
from typing import Callable, Optional

from bmslib.bms import BmsSample
from bmslib.models.jikong import JKBt

CELL_SLOTS = 24

REG_BALANCER = 0x6C


def _f32(buf, i: int) -> float:
    return struct.unpack_from('<f', buf, i)[0]


def _cell_voltages(frame) -> list:
    return [_f32(frame, 6 + 4 * i) for i in range(CELL_SLOTS)]


def looks_like_jk04_status(frame) -> bool:
    """True if a 0x02 frame carries float32 cell voltages (JK04), never for a physical JK02 frame.

    A float32 between 0.5 and 5 V has 0x3F or 0x40 as its top byte. In a JK02 frame the same byte is
    the high byte of an odd cell's u16 mV, 0x3F/0x40 there means > 16 V per cell. A JK02 slot with
    only the even cell set reads as a denormal float and fails the range check."""
    if len(frame) < 300 or frame[4] != 0x02:
        return False
    live = 0
    for i in range(CELL_SLOTS):
        raw = frame[6 + 4 * i:10 + 4 * i]
        if raw == b'\0\0\0\0':
            continue
        v = _f32(frame, 6 + 4 * i)
        if raw[3] not in (0x3F, 0x40) or not 0.5 <= v <= 5.0:
            return False
        live += 1
    return live >= 2


def decode_jk04_status(frame) -> dict:
    u32 = lambda i: int.from_bytes(frame[i:i + 4], 'little')
    return dict(
        cell_voltages=_cell_voltages(frame),
        cell_resistances=[_f32(frame, 102 + 4 * i) for i in range(CELL_SLOTS)],
        average_cell_voltage=_f32(frame, 202),
        delta_cell_voltage=_f32(frame, 206),
        max_voltage_cell=frame[218] + 1,
        min_voltage_cell=frame[219] + 1,
        balancer_state=frame[220],
        balance_current=_f32(frame, 222),
        uptime=u32(286),
    )


def decode_jk04_settings(frame) -> dict:
    return dict(
        cell_count=frame[34],
        power_off_voltage=_f32(frame, 38),
        voltage_calibration=_f32(frame, 74),
        balancing_start_voltage=_f32(frame, 98),
        balance_trigger_voltage=_f32(frame, 106),
        max_balance_current=_f32(frame, 110),
        balancer=bool(frame[114]),
    )


class JK04Bt(JKBt):
    """Old JK active balancer (JK04 protocol). Connect, framing and device info are JKBt's."""

    def _num_cells_from_settings(self, buf) -> int:
        return buf[34]

    def _status_frame_implausible(self, buf) -> Optional[str]:
        # a frame spliced after a dropped notify packet can pass the 8-bit sum (#391)
        cells = _cell_voltages(buf)
        live = [v for v in cells if v]
        if not live:
            return 'no cell voltages'
        if any(not (math.isfinite(v) and 0.5 <= v <= 5.0) for v in live):  # as JK02's 500..5000 mV
            return 'cell voltage out of range: %s' % ['%.3f' % v for v in live]
        if self.num_cells and len(live) > self.num_cells:
            return '%d cell voltages for a %d cell pack' % (len(live), self.num_cells)
        bal = _f32(buf, 222)
        if not (math.isfinite(bal) and abs(bal) <= 50):
            return 'balance current %s A' % bal
        return None

    def _decode_sample(self, buf, t_buf: float, has_float_charger: bool = False) -> BmsSample:
        st = decode_jk04_status(buf)
        entry = self._resp_table.get(0x01)
        switches = dict(balance=bool(entry[0][114])) if entry else None
        return BmsSample(
            voltage=sum(st['cell_voltages']),  # no pack voltage on the wire
            current=math.nan,  # balancer only, no shunt
            balance_current=st['balance_current'],
            switches=switches,
            uptime=float(st['uptime']),
            timestamp=t_buf,
        )

    async def fetch(self, wait=True) -> BmsSample:
        if wait:
            with await self._fetch_futures.acquire_timeout(0x02, timeout=self.TIMEOUT / 2):
                await self._fetch_futures.wait_for(0x02, self.TIMEOUT)

        if 0x01 not in self._resp_table:
            await self._q(cmd=0x96, resp=0x01)  # settings, for the balancer switch

        buf, t_buf = self._resp_table[0x02]
        return self._decode_sample(buf, t_buf)

    async def subscribe(self, callback: Callable[[BmsSample], None]):
        self._callbacks[0x02].append(lambda buf: callback(self._decode_sample(buf, t_buf=time.time())))

    async def fetch_voltages(self):
        if self.num_cells is None:
            raise Exception("num_cells not set")
        buf, _ = self._resp_table[0x02]
        return [round(v * 1000) for v in _cell_voltages(buf)[:self.num_cells]]

    async def has_float_charger(self):
        return False

    async def set_switch(self, switch: str, state: bool):
        if switch != 'balance':
            raise ValueError('JK04 has only the balance switch, not %r' % switch)
        async with self._write_lock:
            await self._settle_after_query()
            self._switch_gen += 1
            await self._write(REG_BALANCER, [0x01 if state else 0x00])  # 1-byte register
            await asyncio.sleep(.2)
        self._resp_table.pop(0x01, None)  # settings frame holds the switch state

    def supports_set_soc(self) -> bool:
        return False

    async def set_soc(self, soc: float):
        raise NotImplementedError("JK04 has no SOC")

    def supports_settings(self) -> bool:
        return False  # the jk_settings register table is JK02_32S

    async def refresh_settings(self) -> dict:
        raise NotImplementedError("JK04 settings are not supported")

    def settings_from_recent_frame(self, max_age_s: float):
        return None
