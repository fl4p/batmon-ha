"""Basen BMS over BLE.

Basen-based LiFePO4 rack/wall packs. Reverse-engineered protocol ported from
syssi/esphome-basen-bms (docs/protocol-design.md + basen_bms_ble.cpp).

BLE:
  service 0xFA00, notify (RX) 0xFA01, control/write (TX) 0xFA02.

Wire format (all multi-byte fields are little-endian):

  0    SOF        0x3A or 0x3B (both appear; responses mix them)
  1    address    0x16
  2    frame type e.g. 0x2A status, 0x2B general info, 0x24/25/26 cell voltages
  3    data_len   N (payload byte count)
  4..  payload    N bytes
  4+N  CRC        u16 LE = sum of bytes[1 .. 3+N]
  6+N  end        0x0D 0x0A

Total frame length is ``data_len + 8``.

Sign convention: Basen reports current as positive=charging. batmon uses
positive=discharge (out of the pack), so the decoded current is negated.

Only reads are implemented (no MOSFET switching). Decoders are covered by unit
tests using the captured example frames from the upstream protocol doc, but
have not been run against live hardware yet.
"""
import asyncio
from typing import List

from bmslib.bms import BmsSample
from bmslib.bt import BtBms

SOF_A = 0x3A
SOF_B = 0x3B
ADDRESS = 0x16
PKT_END = b'\x0d\x0a'

FT_CELL_VOLTAGES_1_12 = 0x24
FT_CELL_VOLTAGES_13_24 = 0x25
FT_STATUS = 0x2A
FT_GENERAL_INFO = 0x2B

MOSFET_ON = 1 << 7  # bit 7 of the charge/discharge state byte = MOSFET closed
# Bits 0-6 of the state bytes [20]/[21] are hard protection trips (overcurrent,
# over/under-temp, cell OV/UV, pack OV, short-circuit, ...); bit 7 is just the
# MOSFET state. Mask keeps the trips, drops the benign MOSFET bit.
PROT_MASK = 0x7F
# Charging-warning byte [22] bit 4 is "Fully charged (FC)" — a benign status,
# NOT a fault. It must be excluded or every full charge raises a false alarm.
CHG_WARN_FULLY_CHARGED = 1 << 4


def _u16(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8)


def _u32(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)


def _s32(b: bytes, i: int) -> int:
    v = _u32(b, i)
    return v - 0x100000000 if v >= 0x80000000 else v


def _checksum(frame: bytes, data_len: int) -> int:
    """Sum of bytes[1 .. data_len+3] (address..end of payload), kept to 16 bits."""
    return sum(frame[1:data_len + 4]) & 0xFFFF


def build_frame(frame_type: int, value: int = 0x00, sof: int = SOF_A) -> bytes:
    """Assemble a Basen read request. data_len is always 1 (a single value byte)."""
    body = bytes([sof, ADDRESS, frame_type, 0x01, value])
    crc = sum(body[1:]) & 0xFFFF  # bytes[1..4] == address, type, len, value
    return body + bytes([crc & 0xFF, crc >> 8]) + PKT_END


def parse_frame(frame: bytes) -> dict:
    """Validate a complete Basen frame and return {frame_type, data}.

    ``data`` is the full frame (indexed exactly like the protocol doc, i.e.
    ``data[4]`` is the first payload byte). Raises ValueError on any structural
    or checksum failure so a corrupt frame can never decode to a zero reading.
    """
    if len(frame) < 8:
        raise ValueError(f"basen frame too short: {len(frame)} bytes")
    if frame[0] not in (SOF_A, SOF_B):
        raise ValueError(f"basen frame bad SOF: 0x{frame[0]:02X}")
    if frame[1] != ADDRESS:
        raise ValueError(f"basen frame bad address: 0x{frame[1]:02X}")

    data_len = frame[3]
    expected = data_len + 8
    if len(frame) != expected:
        raise ValueError(f"basen frame length mismatch: header wants {expected}, got {len(frame)}")
    if frame[-2:] != PKT_END:
        raise ValueError("basen frame missing 0x0D 0x0A terminator")

    recv_crc = _u16(frame, data_len + 4)
    calc_crc = _checksum(frame, data_len)
    if recv_crc != calc_crc:
        raise ValueError(f"basen checksum mismatch: got 0x{recv_crc:04X}, want 0x{calc_crc:04X}")

    return dict(frame_type=frame[2], data=frame)


def decode_status(d: bytes) -> dict:
    """Decode a status frame (0x2A). ``d`` is the full frame (SOF at index 0)."""
    current = _s32(d, 4) * 0.001            # +charging (Basen convention)
    total_v = _u32(d, 8) * 0.001
    temps = [float((d[12 + i] - 256) if d[12 + i] >= 128 else d[12 + i]) for i in range(4)]
    remaining_ah = _u32(d, 16) * 0.001
    charge_states = d[20]
    discharge_states = d[21]
    charge_mosfet = bool(charge_states & MOSFET_ON)
    discharge_mosfet = bool(discharge_states & MOSFET_ON)
    charge_warn = d[22]
    discharge_warn = d[23]
    soc = d[24]

    # Fold the hard protection trips (state bits 0-6) AND the soft warnings into
    # one code so `problem` reflects a real trip, not only a warning. A pure
    # MOSFET-on/off state (bit 7) and the benign "fully charged" warning do NOT
    # count. Layout: [prot_chg | prot_dsg<<8 | warn_chg<<16 | warn_dsg<<24].
    problem_code = (
        (charge_states & PROT_MASK)
        | ((discharge_states & PROT_MASK) << 8)
        | ((charge_warn & ~CHG_WARN_FULLY_CHARGED) << 16)
        | (discharge_warn << 24)
    )
    return dict(
        current_charging=current,
        total_v=total_v,
        temps_c=temps,
        remaining_ah=remaining_ah,
        charge_mosfet=charge_mosfet,
        discharge_mosfet=discharge_mosfet,
        soc=soc,
        problem_code=problem_code,
        problem=bool(problem_code),
    )


def decode_general_info(d: bytes) -> dict:
    """Decode a general-info frame (0x2B). ``d`` is the full frame."""
    return dict(
        nominal_ah=_u32(d, 4) * 0.001,
        nominal_v=_u32(d, 8) * 0.001,
        real_ah=_u32(d, 12) * 0.001,
        cycles=_u16(d, 26),
    )


def decode_cell_voltages(d: bytes) -> List[int]:
    """Decode a cell-voltage chunk (0x24/0x25/0x26) into a list of mV ints.

    Trailing zero slots (unpopulated cells in the fixed-width chunk) are kept as
    zeros; the caller trims them once all chunks are concatenated.
    """
    data_len = d[3]
    ncells = data_len // 2
    return [_u16(d, 4 + i * 2) for i in range(ncells)]


class BasenBt(BtBms):
    UUID_RX = '0000fa01-0000-1000-8000-00805f9b34fb'  # notify
    UUID_TX = '0000fa02-0000-1000-8000-00805f9b34fb'  # control/write
    TIMEOUT = 12

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_cells: List[int] = []
        self._last_temps: List[float] = []
        self._cell_count = 0  # high-water mark, see fetch_voltages

    def _notification_handler(self, sender, data):
        self._buffer += bytes(data)
        while self._buffer:
            # Resync to the next start-of-frame byte, keeping any partial frame.
            if self._buffer[0] not in (SOF_A, SOF_B):
                nxt = min((i for i in (self._buffer.find(SOF_A), self._buffer.find(SOF_B)) if i >= 0),
                          default=-1)
                if nxt < 0:
                    self._buffer.clear()
                    return
                del self._buffer[:nxt]
            if len(self._buffer) < 4:
                return  # need data_len

            frame_len = self._buffer[3] + 8
            if len(self._buffer) < frame_len:
                return  # frame still incomplete

            frame = bytes(self._buffer[:frame_len])
            try:
                fields = parse_frame(frame)
            except ValueError as exc:
                # Drop just the SOF and resync — the length byte may have been noise.
                self.logger.warning("discarding invalid basen frame: %s", exc)
                del self._buffer[:1]
                continue

            del self._buffer[:frame_len]
            self._fetch_futures.set_result(fields['frame_type'], fields['data'])

    async def connect(self, **kwargs):
        await super().connect(**kwargs)
        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        await self.stop_notify(self.UUID_RX)
        await super().disconnect()

    async def _read(self, frame_type: int) -> bytes:
        with self._fetch_futures.acquire(frame_type):
            await self.client.write_gatt_char(self.UUID_TX, data=build_frame(frame_type))
            return await self._fetch_futures.wait_for(frame_type, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        status = decode_status(await self._read(FT_STATUS))

        # General info (capacity, cycles) is best-effort; a failure there must
        # not drop the status reading.
        info = None
        try:
            info = decode_general_info(await self._read(FT_GENERAL_INFO))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("basen general-info read failed, using status only: %s", exc)

        capacity = info['nominal_ah'] if info else float('nan')
        if info and info['nominal_ah'] > 0:
            soh = min(100.0, info['real_ah'] / info['nominal_ah'] * 100)
        else:
            soh = float('nan')

        self._last_temps = status['temps_c']

        return BmsSample(
            voltage=status['total_v'],
            current=-status['current_charging'],  # negate: batmon +current == discharge
            charge=status['remaining_ah'],
            capacity=capacity,
            soc=status['soc'],
            soh=soh,
            num_cycles=info['cycles'] if info else float('nan'),
            temperatures=status['temps_c'],
            switches=dict(charge=status['charge_mosfet'], discharge=status['discharge_mosfet']),
            battery_charging=status['current_charging'] > 0,
            problem=status['problem'],
            problem_code=status['problem_code'],
        )

    async def fetch_voltages(self) -> List[int]:
        cells = decode_cell_voltages(await self._read(FT_CELL_VOLTAGES_1_12))
        try:
            cells += decode_cell_voltages(await self._read(FT_CELL_VOLTAGES_13_24))
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("basen cells 13-24 unavailable: %s", exc)
        # The chunks are fixed-width (12 slots each) and pad unpopulated cells
        # with 0. We must NOT value-trim every trailing zero: a genuine dead /
        # disconnected cell also reads 0 mV and, if it's the last real cell,
        # would be indistinguishable from padding and silently dropped (hiding a
        # safety-critical fault — the same reason jbd.py forbids this).
        #
        # Instead, learn the populated cell count as a monotonic high-water mark
        # from healthy polls. Once we've seen N cells, we always return N, so a
        # later 0 at an established position is preserved as a real reading, not
        # trimmed away. Only genuine trailing padding beyond the high-water mark
        # is dropped.
        populated = len(cells)
        while populated and cells[populated - 1] == 0:
            populated -= 1
        self._cell_count = max(self._cell_count, populated)
        cells = cells[:self._cell_count]
        self._last_cells = cells
        return cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps


async def main():
    bms = BasenBt('AA:BB:CC:DD:EE:FF', name='basen')
    await bms.connect()
    print(await bms.fetch())
    print(await bms.fetch_voltages())
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
