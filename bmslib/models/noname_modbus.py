"""Generic Modbus-RTU-over-BLE BMS (Nordic UART Service transport).

Reverse-engineered from https://github.com/fl4p/batmon-ha/issues/131 — a
no-name Chinese BMS that wraps standard Modbus RTU function 0x03 reads
inside the Nordic UART characteristics:

    UUID_RX (notify) = 6e400003-b5a3-f393-e0a9-e50e24dcca9e
    UUID_TX (write)  = 6e400002-b5a3-f393-e0a9-e50e24dcca9e

Three reads cover everything:
  0x231C × 4 regs   → cell_count, temp_count, ?, ?
  0xD000 × 38 regs  → 32 cell mV (BE) + max/min mV + max/min idx + delta mV
                      + pack centivolts. Unused cell slots read back as 0xEE49.
  0xD026 × 25 regs  → temp1 (°C×10 + 400), 3× duplicate temps (zero-padded),
                      charge A (×10), discharge A (×10), SoC, SoH,
                      remaining/full/design Ah (×10), cycle count.
"""

import asyncio
import math

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


SLAVE_ID = 0x01
FN_READ_HOLDING = 0x03

REG_INFO = 0x231C       # 4 regs
REG_VOLTS = 0xD000      # 38 regs
REG_CAP = 0xD026        # 25 regs

CELL_SLOT_EMPTY = 0xEE49


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


def build_read(addr: int, n: int, slave: int = SLAVE_ID) -> bytes:
    body = bytes([slave, FN_READ_HOLDING,
                  (addr >> 8) & 0xFF, addr & 0xFF,
                  (n >> 8) & 0xFF, n & 0xFF])
    crc = crc16_modbus(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def parse_response(buf: bytes, slave: int = SLAVE_ID,
                   fn: int = FN_READ_HOLDING) -> bytes:
    """Validate a Modbus RTU read response and return its payload bytes."""
    if len(buf) < 5:
        raise ValueError(f"modbus frame too short: {bytes(buf).hex()}")
    if buf[0] != slave or buf[1] != fn:
        raise ValueError(f"modbus header mismatch: {bytes(buf[:2]).hex()}")
    n = buf[2]
    total = 3 + n + 2
    if len(buf) < total:
        raise ValueError(f"modbus frame truncated: have {len(buf)}, need {total}")
    body = bytes(buf[:3 + n])
    crc_rx = buf[3 + n] | (buf[3 + n + 1] << 8)
    crc_calc = crc16_modbus(body)
    if crc_rx != crc_calc:
        raise ValueError(f"modbus CRC: got {crc_rx:04x}, want {crc_calc:04x}")
    return body[3:]


def _u16_be(b, off):
    return (b[off] << 8) | b[off + 1]


def decode_info(payload: bytes):
    return _u16_be(payload, 0), _u16_be(payload, 2)   # (cell_count, temp_count)


def decode_voltages(payload: bytes, cell_count: int = 0):
    cells_mv = [_u16_be(payload, i * 2) for i in range(32)]
    if cell_count > 0:
        cells_mv = cells_mv[:cell_count]
    else:
        cells_mv = [v for v in cells_mv if v not in (CELL_SLOT_EMPTY, 0)]
    pack_cv = _u16_be(payload, 74)  # reg index 37 (last) — pack centivolts
    return cells_mv, pack_cv


def decode_capacity(payload: bytes):
    # Temperatures: the example frame in #131 shows reg 0 plus three identical
    # readings further in (regs 9-11). Until a multi-NTC sample is available
    # we only expose the first — extra slots can be added once we see them.
    temp_raw = _u16_be(payload, 0)
    temp_c = (temp_raw - 400) / 10.0 if temp_raw else math.nan

    return dict(
        temp_c=temp_c,
        charge_a=_u16_be(payload, 24) / 10.0,
        discharge_a=_u16_be(payload, 26) / 10.0,
        soc=float(_u16_be(payload, 28)),
        soh=float(_u16_be(payload, 30)),
        remaining_ah=_u16_be(payload, 32) / 10.0,
        full_ah=_u16_be(payload, 34) / 10.0,
        design_ah=_u16_be(payload, 36) / 10.0,
        cycles=float(_u16_be(payload, 38)),
    )


class NoNameModbusBt(BtBms):
    UUID_RX = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'
    UUID_TX = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._cells_mv = []
        self._cell_count = 0
        self._temp_count = 1

    def _notification_handler(self, sender, data):
        self._buffer += data
        if self.verbose_log:
            self.logger.debug("rx %s (buf=%d)", bytes(data).hex(), len(self._buffer))

        if len(self._buffer) < 3:
            return
        expected = 3 + self._buffer[2] + 2
        if len(self._buffer) < expected:
            return

        frame = bytes(self._buffer[:expected])
        del self._buffer[:expected]
        self._fetch_futures.set_result(FN_READ_HOLDING, frame)

    async def connect(self, **kwargs):
        await super().connect(**kwargs)
        await self.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        self._fetch_futures.clear()
        await super().disconnect()

    async def _q(self, addr: int, n: int) -> bytes:
        cmd = build_read(addr, n)
        with self._fetch_futures.acquire(FN_READ_HOLDING):
            self._buffer.clear()
            if self.verbose_log:
                self.logger.debug("tx %s (addr=0x%04x n=%d)", cmd.hex(), addr, n)
            await self.client.write_gatt_char(self.UUID_TX, data=cmd)
            frame = await self._fetch_futures.wait_for(FN_READ_HOLDING, self.TIMEOUT)
        return parse_response(frame)

    async def fetch(self) -> BmsSample:
        info = await self._q(REG_INFO, 4)
        self._cell_count, self._temp_count = decode_info(info)

        v_payload = await self._q(REG_VOLTS, 38)
        self._cells_mv, pack_cv = decode_voltages(v_payload, self._cell_count)

        cap = await self._q(REG_CAP, 25)
        c = decode_capacity(cap)

        # batmon convention: + current = discharging, - = charging
        current = c["discharge_a"] - c["charge_a"]

        return BmsSample(
            voltage=pack_cv / 100.0,
            current=current,
            soc=c["soc"],
            soh=c["soh"],
            charge=c["remaining_ah"],
            capacity=c["design_ah"],
            aged_capacity=c["full_ah"],
            num_cycles=c["cycles"],
            temperatures=[c["temp_c"]] if not math.isnan(c["temp_c"]) else [],
        )

    async def fetch_voltages(self):
        return list(self._cells_mv)


async def main():
    import sys
    bms = NoNameModbusBt(sys.argv[1], name='noname_modbus', verbose_log=True)
    await bms.connect()
    try:
        print(await bms.fetch())
        print(await bms.fetch_voltages())
    finally:
        await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
