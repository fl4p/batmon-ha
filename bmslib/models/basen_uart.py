"""Basen BMS over RS232 / RS485 (wired).

This is the wired protocol spoken by Basen packs on their RS485 port — a
DIFFERENT protocol from the Basen BLE app (that one is `basen`, see
bmslib/models/basen.py). Ported from GHswitt/esphome-basen.

Frame:  SOI(0x7E) ADDR CMD LEN <data[LEN]> CHK EOI(0x0D)
  CHK is a single byte over SOI..end-of-data: (XOR_of_all ^ SUM_of_all) & 0xFF.
  A request has LEN data bytes (0 for a plain read); the response echoes ADDR
  and CMD and carries LEN payload bytes.

The INFO command (0x01) returns a 0x7C-byte payload: a version byte, a
cell-voltage block (count-prefixed, each cell a big-endian u16 with the top bit
flagging balancing and the low 12 bits the mV), then a series of type/count/
value blocks (current, SoC, capacity, temperatures, a multi-byte status
bitmask, cycles, total voltage, SoH, ...).

Current direction, MOSFET state and alarms all come from the status bitmask,
whose bits are classified by `_STATUS_BITS` as benign status / alarm / fault —
so a "Charging"/"Discharging"/"Heating"/manual-MOS status bit never raises a
false alarm (the same benign-bit trap the BLE model documents).

Decode is validated against the real INFO frame published in the GHswitt README
(unit test), but has not been run against live hardware — see issue #276.

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    basen_uart
    alias:   any human-readable name
"""
import asyncio
from typing import List

from bmslib.bms import BmsSample
from bmslib.bt import BtBms

SOI = 0x7E
EOI = 0x0D

CMD_INFO = 0x01
INFO_LEN = 0x7C          # fixed payload length of a valid INFO response
INFO_VERSION = 0x01

# Temperature-sensor sub-types inside the 0x05 block.
TEMP_TYPE_MOS = 0x40
TEMP_TYPE_AMBIENT = 0x20

# Status-bitmask bits: (byte_offset, bit_mask, kind, error_mask). kind 0 = benign
# status, 1 = alarm, 2 = fault. Ported verbatim from GHswitt's status_messages[].
# Only kind >= 1 bits feed `problem`; kind 0 bits (Charging/Discharging/manual
# MOS/heating/...) must not, or normal operation would read as a fault.
_STATUS_BITS = [
    (0, 0x08, 2, 0x0004), (0, 0x10, 2, 0x0200), (0, 0x20, 2, 0x0801),
    (0, 0x40, 2, 0x0801), (0, 0x80, 2, 0x0800),
    (1, 0x01, 2, 0x0800), (1, 0x02, 2, 0x0800), (1, 0x04, 2, 0x0200),
    (1, 0x08, 2, 0x0008), (1, 0x10, 1, 0x0001), (1, 0x20, 1, 0x0001),
    (1, 0x40, 1, 0x0001), (1, 0x80, 2, 0x0801),
    (2, 0x01, 2, 0x0008), (2, 0x02, 2, 0x0010), (2, 0x10, 2, 0x0001),
    (2, 0x20, 2, 0x0001), (2, 0x40, 1, 0x0800),
    (3, 0x01, 0, 0x0000), (3, 0x02, 0, 0x0000),  # Charging / Discharging
    (3, 0x04, 2, 0x0400), (3, 0x08, 2, 0x0080), (3, 0x10, 2, 0x0002),
    (3, 0x40, 2, 0x0020), (3, 0x80, 2, 0x0040),
    (4, 0x01, 2, 0x0010), (4, 0x02, 2, 0x0008), (4, 0x10, 2, 0x0001),
    (5, 0x01, 0, 0x0001), (5, 0x02, 0, 0x0001), (5, 0x04, 0, 0x0001),
    (5, 0x08, 0, 0x0001), (5, 0x10, 0, 0x0000), (5, 0x20, 2, 0x0008),
    (5, 0x40, 2, 0x0010), (5, 0x80, 2, 0x0010),
    (7, 0x01, 1, 0x0001), (7, 0x02, 1, 0x0001), (7, 0x04, 0, 0x0001),
    (7, 0x08, 2, 0x0001), (7, 0x10, 1, 0x0001), (7, 0x20, 2, 0x0800),
    (7, 0x40, 1, 0x0001), (7, 0x80, 1, 0x0001),
    (8, 0x01, 1, 0x0008), (8, 0x02, 1, 0x0010), (8, 0x04, 1, 0x0008),
    (8, 0x08, 1, 0x0001), (8, 0x10, 1, 0x1000), (8, 0x20, 1, 0x0008),
    (8, 0x40, 1, 0x0010),
    (9, 0x01, 1, 0x0002), (9, 0x02, 1, 0x0004), (9, 0x04, 1, 0x0002),
    (9, 0x08, 1, 0x0004), (9, 0x10, 1, 0x0100), (9, 0x20, 1, 0x0080),
    (9, 0x40, 1, 0x0020), (9, 0x80, 1, 0x0040),
]
# Direction / MOSFET-state bits (from the same table).
_SB_CHARGING = (3, 0x01)
_SB_DISCHARGING = (3, 0x02)
_SB_CHG_MOS_OFF = (2, 0x10)
_SB_DISC_MOS_OFF = (2, 0x20)


def _checksum(data: bytes) -> int:
    """Basen wired checksum: (XOR of all bytes) XOR (SUM of all bytes), 8-bit."""
    x = 0
    s = 0
    for b in data:
        x ^= b
        s += b
    return (x ^ s) & 0xFF


def build_frame(addr: int, cmd: int, data: bytes = b'') -> bytes:
    """Assemble a Basen wired request frame."""
    body = bytes([SOI, addr, cmd, len(data)]) + data
    return body + bytes([_checksum(body), EOI])


def parse_frame(frame: bytes) -> dict:
    """Validate a complete wired frame and return {addr, cmd, data}.

    Raises ValueError on any structural or checksum failure so a corrupt frame
    can never decode to a zero reading.
    """
    if len(frame) < 6:
        raise ValueError(f"basen wired frame too short: {len(frame)} bytes")
    if frame[0] != SOI:
        raise ValueError(f"basen wired frame bad SOI: 0x{frame[0]:02X}")
    length = frame[3]
    expected = length + 6  # SOI+addr+cmd+len (4) + data + chk + EOI
    if len(frame) != expected:
        raise ValueError(f"basen wired length mismatch: header wants {expected}, got {len(frame)}")
    if frame[-1] != EOI:
        raise ValueError(f"basen wired frame bad EOI: 0x{frame[-1]:02X}")

    body = frame[:-2]  # SOI..end-of-data
    recv = frame[-2]
    calc = _checksum(body)
    if recv != calc:
        raise ValueError(f"basen wired checksum mismatch: got 0x{recv:02X}, want 0x{calc:02X}")

    return dict(addr=frame[1], cmd=frame[2], data=frame[4:4 + length])


def _be16(b: bytes, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def decode_status_bitmask(bm: bytes) -> dict:
    """Classify a status bitmask into direction, MOSFET state and alarms.

    ``problem`` counts only alarm/fault bits; benign status bits are ignored.
    """
    def bit(ob):
        off, msk = ob
        return off < len(bm) and bool(bm[off] & msk)

    problem_code = 0
    for off, msk, kind, err in _STATUS_BITS:
        if kind >= 1 and off < len(bm) and (bm[off] & msk):
            problem_code |= err

    return dict(
        charging=bit(_SB_CHARGING),
        discharging=bit(_SB_DISCHARGING),
        charge_mosfet=not bit(_SB_CHG_MOS_OFF),
        discharge_mosfet=not bit(_SB_DISC_MOS_OFF),
        problem_code=problem_code,
        problem=bool(problem_code),
    )


def decode_info(data: bytes) -> dict:
    """Decode a COMMAND_INFO (0x01) payload.

    Returns physical quantities plus the raw status bitmask. Current is returned
    as the pack's own signed value (``current_a``, +charging like GHswitt);
    direction is resolved by the caller from the status bits.
    """
    if len(data) != INFO_LEN:
        raise ValueError(f"basen INFO wrong length: {len(data)} (want {INFO_LEN})")
    if data[0] != INFO_VERSION:
        raise ValueError(f"basen INFO bad version: 0x{data[0]:02X}")

    pos = 1

    # Cell-voltage block: count-prefixed, each cell a big-endian u16
    # (bit15 = balancing, low 12 bits = mV).
    num_cells = data[pos]
    if not 1 <= num_cells <= 32:
        raise ValueError(f"basen INFO implausible cell count {num_cells}")
    if pos + 1 + num_cells * 2 > len(data):
        raise ValueError("basen INFO truncated in cell-voltage block")
    cell_mv: List[int] = []
    balancing = 0
    for i in range(num_cells):
        raw = _be16(data, pos + 1 + i * 2)
        if raw & 0x8000:
            balancing |= (1 << i)
        cell_mv.append(raw & 0x0FFF)
    pos += 1 + num_cells * 2

    out = dict(
        cell_mv=cell_mv, balancing=balancing,
        current_a=float('nan'), soc=float('nan'), capacity_ah=float('nan'),
        temps_c=[], mos_temp=float('nan'), ambient_temp=float('nan'),
        cycles=float('nan'), voltage=float('nan'), soh=float('nan'),
        status_bitmask=b'',
    )

    # type/count/value blocks
    while pos < len(data):
        if pos + 2 > len(data):
            raise ValueError("basen INFO truncated at block header")
        btype = data[pos]
        count = data[pos + 1]
        size = 2 if btype <= 0x0A else 4
        block = 2 + count * size
        if pos + block > len(data):
            raise ValueError(f"basen INFO truncated in block 0x{btype:02X}")
        v = _be16(data, pos + 2) if count else 0

        if btype == 0x02:      # current: 300 - v/100 (A), +charging
            out['current_a'] = 300.0 - v / 100.0
        elif btype == 0x03:    # SoC %
            out['soc'] = v / 100.0
        elif btype == 0x04:    # capacity remaining (Ah)
            out['capacity_ah'] = v / 100.0
        elif btype == 0x05:    # temperatures: [sub_type, raw-50] per sensor
            normals = []
            for i in range(count):
                st = data[pos + 2 + 2 * i]
                val = data[pos + 2 + 2 * i + 1] - 50
                if st == TEMP_TYPE_MOS:
                    out['mos_temp'] = float(val)
                elif st == TEMP_TYPE_AMBIENT:
                    out['ambient_temp'] = float(val)
                else:
                    normals.append(float(val))
            out['temps_c'] = normals
        elif btype == 0x06:    # status bitmask
            out['status_bitmask'] = bytes(data[pos + 2:pos + 2 + count * size])
        elif btype == 0x07:    # cycles
            out['cycles'] = v
        elif btype == 0x08:    # total voltage (V)
            out['voltage'] = v / 100.0
        elif btype == 0x09:    # SoH %
            out['soh'] = v / 100.0
        pos += block

    if sum(cell_mv) == 0:
        raise ValueError("basen INFO: all cell voltages zero, invalid frame")
    return out


class BasenUart(BtBms):
    """Basen BMS over an RS232 / RS485 (USB-UART) adapter."""

    BAUDRATE = 9600  # 9600 8N1 per GHswitt
    SERIAL_KWARGS = dict(eol=bytes([EOI]), timeout=2)

    ADR = 0x01
    TIMEOUT = 16
    _KEY = 0

    def __init__(self, address, **kwargs):
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
            if len(self._buffer) < 4:
                return  # need the length byte
            frame_len = self._buffer[3] + 6
            if len(self._buffer) < frame_len:
                return  # incomplete
            frame = bytes(self._buffer[:frame_len])
            try:
                fields = parse_frame(frame)
            except ValueError as exc:
                self.logger.warning("discarding invalid basen wired frame: %s", exc)
                del self._buffer[:1]  # resync past this SOI
                continue
            del self._buffer[:frame_len]
            # Ignore short command echoes / acks (no payload); only deliver a
            # data-carrying response.
            if fields['data']:
                self._fetch_futures.set_result(self._KEY, fields)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("basen-uart", "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def disconnect(self):
        await self.stop_notify(self.UUID_RX)
        await super().disconnect()

    async def _read(self, cmd: int) -> dict:
        with self._fetch_futures.acquire(self._KEY):
            await self.client.write_gatt_char(self.UUID_TX, data=build_frame(self.ADR, cmd))
            fields = await self._fetch_futures.wait_for(self._KEY, self.TIMEOUT)
        if fields['addr'] != self.ADR:
            raise ValueError(f"basen response address mismatch: 0x{fields['addr']:02X}")
        if fields['cmd'] != cmd:
            raise ValueError(f"basen response cmd mismatch: 0x{fields['cmd']:02X} != 0x{cmd:02X}")
        return fields

    async def fetch(self) -> BmsSample:
        info = decode_info((await self._read(CMD_INFO))['data'])
        st = decode_status_bitmask(info['status_bitmask'])

        # batmon convention: current > 0 == discharge. Magnitude from the analog
        # field, direction from the status bits (authoritative). When neither
        # bit is set (idle) the near-zero magnitude keeps its raw sign.
        mag = abs(info['current_a'])
        if st['discharging'] and not st['charging']:
            current = mag
        elif st['charging'] and not st['discharging']:
            current = -mag
        else:
            current = info['current_a']

        # Ambient probe appended so it isn't lost (batmon has no ambient field).
        temps = list(info['temps_c'])
        if info['ambient_temp'] == info['ambient_temp']:  # not nan
            temps.append(info['ambient_temp'])

        self._last_cells = info['cell_mv']
        self._last_temps = temps

        return BmsSample(
            voltage=info['voltage'],
            current=current,
            charge=info['capacity_ah'],   # remaining Ah; capacity derived from soc
            soc=info['soc'],
            soh=info['soh'],
            num_cycles=info['cycles'],
            temperatures=temps,
            mos_temperature=info['mos_temp'],
            balance_current=float('nan'),
            switches=dict(charge=st['charge_mosfet'], discharge=st['discharge_mosfet']),
            battery_charging=st['charging'] or None,
            problem=st['problem'],
            problem_code=st['problem_code'],
        )

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps


async def main():
    bms = BasenUart('serial', name='basen', adapter='/dev/ttyUSB0')
    await bms.connect()
    print(await bms.fetch())
    print(await bms.fetch_voltages())
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
