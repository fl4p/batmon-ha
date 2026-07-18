"""PACE BMS "paceic" protocol over RS232 / RS485 (wired).

This is the ASCII protocol spoken by PACE-based server-rack packs (SOK,
SunGoldPower, Sunsynk, many 48V rebrands) on their RS232/RS485 ports and by
the PbmsTools PC software. It is *unrelated* to the binary BLE protocol of the
PACEEX/PeiCheng phone app (that one is handled by aiobmsble's ``pace_bms``,
reachable in batmon as ``type: pace_aiobmsble``).

Wire format (everything between SOI and CHKSUM is ASCII-hex *text*, so each
logical byte is two characters and each u16 is four):

  SOI  VER  ADR  CID1  CID2  LENGTH   INFO            CHKSUM  EOI
  7E   xx   xx   xx    xx    xxxx     ...              xxxx    0D
  '~'  25   01   46    42    E002     01               FD30    '\r'

  VER    protocol version (0x25 here; 0x20 exists on other firmware)
  ADR    pack address
  CID1   device/command group (0x46 for LiFePO4 rack packs)
  CID2   function code (request) / return-code (response, 0x00 == OK)
  LENGTH high nibble = LCHKSUM over the 3 LENID nibbles; low 12 bits = LENID
         = number of ASCII chars in INFO
  CHKSUM two's-complement of the ASCII-byte sum of VER..INFO (see _checksum)

Only the two read commands are implemented (0x42 analog, 0x44 status); no
writes/switches. Sources — framing, register layout and both example frames
below are ported from nkinnan/esphome-pace-bms (protocol v25) and cross-checked
against syssi/esphome-pace-bms and the PACE RS232 protocol PDF (2018-07-05).
The analog and status decoders are covered by unit tests using real captured
frames, but have not yet been run against live hardware — see issue #276.

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    pace_uart
    alias:   any human-readable name
"""
import asyncio
from typing import List

from bmslib.bms import BmsSample
from bmslib.bt import BtBms

SOI = 0x7E  # '~'
EOI = 0x0D  # '\r'

CID2_READ_ANALOG = 0x42
CID2_READ_STATUS = 0x44
CID2_OK = 0x00

# System-status byte (SF_) bit meanings, from pace_bms_protocol_v25.h.
SF_CHARGING = 1 << 5
SF_DISCHARGING = 1 << 3
SF_DISCHARGE_MOSFET_ON = 1 << 2
SF_CHARGE_MOSFET_ON = 1 << 1

# Protection-status-2 bit 7 is NOT a fault: it flags "pack fully charged, SoC
# and full-capacity re-learned". It must be excluded from the problem bitmask,
# otherwise every full charge would raise a false alarm.
P2F_FULLY_CHARGED = 1 << 7

# Temperatures are reported in 0.1 K; 2730 = 273.0 K = 0 °C (per nkinnan).
TEMP_ZERO_TENTHS_K = 2730


def _lchksum(lenid: int) -> int:
    """4-bit checksum over the three hex nibbles of a 12-bit LENID."""
    if not 0 <= lenid <= 0xFFF:
        raise ValueError(f"paceic LENID out of range: {lenid}")
    s = (lenid & 0xF) + ((lenid >> 4) & 0xF) + ((lenid >> 8) & 0xF)
    return ((~s) + 1) & 0xF


def _checksum(payload: bytes) -> int:
    """paceic frame checksum: two's complement of the ASCII-byte sum of
    VER..INFO, kept to 16 bits. ``payload`` is the text between SOI and CHKSUM."""
    return (0x10000 - (sum(payload) & 0xFFFF)) & 0xFFFF


def build_frame(ver: int, adr: int, cid1: int, cid2: int, info: bytes = b'') -> bytes:
    """Assemble a complete paceic request frame (ready to write to the port)."""
    lenid = len(info)
    length = (_lchksum(lenid) << 12) | lenid
    body = b'%02X%02X%02X%02X%04X' % (ver, adr, cid1, cid2, length) + info
    return bytes([SOI]) + body + b'%04X' % _checksum(body) + bytes([EOI])


def parse_frame(frame: bytes) -> dict:
    """Validate a complete paceic frame and return its decoded header + INFO.

    Raises ValueError on any structural or checksum failure. A malformed or
    truncated frame must never decode to a zero/empty reading — the caller must
    see the error, not a fabricated sample.
    """
    if len(frame) < 18:  # SOI + 12 header chars + 4 chksum + EOI
        raise ValueError(f"paceic frame too short: {len(frame)} bytes")
    if frame[0] != SOI:
        raise ValueError("paceic frame missing SOI (~)")
    if frame[-1] != EOI:
        raise ValueError("paceic frame missing EOI (\\r)")

    body = frame[1:-5]        # VER..INFO (ASCII)
    chk_field = frame[-5:-1]  # 4 ASCII-hex chars
    try:
        recv_chk = int(chk_field, 16)
    except ValueError:
        raise ValueError(f"paceic checksum field not hex: {chk_field!r}")
    calc_chk = _checksum(body)
    if recv_chk != calc_chk:
        raise ValueError(f"paceic checksum mismatch: got 0x{recv_chk:04X}, want 0x{calc_chk:04X}")

    try:
        ver = int(body[0:2], 16)
        adr = int(body[2:4], 16)
        cid1 = int(body[4:6], 16)
        cid2 = int(body[6:8], 16)
        length = int(body[8:12], 16)
    except ValueError:
        raise ValueError(f"paceic header not hex: {body[:12]!r}")

    lenid = length & 0xFFF
    if _lchksum(lenid) != (length >> 12):
        raise ValueError("paceic LENGTH checksum (LCHKSUM) mismatch")
    info = body[12:]
    if len(info) != lenid:
        raise ValueError(f"paceic INFO length mismatch: header says {lenid}, got {len(info)}")

    return dict(ver=ver, adr=adr, cid1=cid1, cid2=cid2, info=info)


class _Cursor:
    """Walks an ASCII-hex INFO field, decoding one field at a time. Bounds are
    checked on every read so a short frame raises instead of returning junk."""

    def __init__(self, info: bytes):
        self._s = info
        self.pos = 0  # in characters

    def _take(self, nchars: int) -> int:
        end = self.pos + nchars
        if end > len(self._s):
            raise ValueError("paceic INFO truncated while decoding")
        chunk = self._s[self.pos:end]
        try:
            v = int(chunk, 16)
        except ValueError:
            raise ValueError(f"paceic INFO field not hex: {chunk!r}")
        self.pos = end
        return v

    def u8(self) -> int:
        return self._take(2)

    def u16(self) -> int:
        return self._take(4)

    def s16(self) -> int:
        v = self._take(4)
        return v - 0x10000 if v >= 0x8000 else v

    def at_end(self) -> bool:
        return self.pos == len(self._s)


def decode_analog(info: bytes) -> dict:
    """Decode a CID2=0x42 (analog information) INFO payload.

    Returns raw physical quantities in SI-ish units (V, A, Ah, °C). Sign of the
    current is as-reported by the pack and is NOT reinterpreted here; the caller
    resolves charge/discharge direction from the status frame.
    """
    c = _Cursor(info)
    c.u8()               # SPEC BUG: leading 0x00 seen on the wire before busId
    pack_id = c.u8()
    cell_count = c.u8()
    if not 1 <= cell_count <= 16:
        raise ValueError(f"paceic analog: implausible cell count {cell_count}")
    cell_mv = [c.u16() for _ in range(cell_count)]

    temp_count = c.u8()
    if not 1 <= temp_count <= 8:
        raise ValueError(f"paceic analog: implausible temp count {temp_count}")
    temps_c = [(c.u16() - TEMP_ZERO_TENTHS_K) / 10 for _ in range(temp_count)]

    current_a = c.s16() * 10 / 1000       # wire is 10 mA units -> A
    total_v = c.u16() / 1000              # mV -> V
    remaining_ah = c.u16() * 10 / 1000    # 10 mAh units -> Ah
    c.u8()                                # protocol-variant constant (UserDefinedValue)
    full_ah = c.u16() * 10 / 1000
    cycles = c.u16()
    design_ah = c.u16() * 10 / 1000

    if sum(cell_mv) == 0:
        raise ValueError("paceic analog: all cell voltages zero, invalid frame")

    return dict(
        pack_id=pack_id,
        cell_mv=cell_mv,
        temps_c=temps_c,
        current_a=current_a,
        total_v=total_v,
        remaining_ah=remaining_ah,
        full_ah=full_ah,
        design_ah=design_ah,
        cycles=cycles,
    )


def decode_status(info: bytes) -> dict:
    """Decode a CID2=0x44 (status/warning) INFO payload into alarm/switch state.

    ``problem_code`` packs the hard-trip protection + fault bytes (excluding the
    "fully charged" status bit). ``problem`` additionally reflects the soft
    warning bytes. A structurally short frame raises rather than reading as
    "no alarms".
    """
    c = _Cursor(info)
    c.u8()               # leading 0x00
    pack_id = c.u8()

    cell_count = c.u8()
    if not 1 <= cell_count <= 16:
        raise ValueError(f"paceic status: implausible cell count {cell_count}")
    cell_warn = [c.u8() for _ in range(cell_count)]

    temp_count = c.u8()
    if not 1 <= temp_count <= 8:
        raise ValueError(f"paceic status: implausible temp count {temp_count}")
    temp_warn = [c.u8() for _ in range(temp_count)]

    charge_current_warn = c.u8()
    total_voltage_warn = c.u8()
    discharge_current_warn = c.u8()
    protect1 = c.u8()
    protect2 = c.u8()
    system = c.u8()
    config = c.u8()          # noqa: F841 (control/config flags, unused for now)
    fault = c.u8()
    balancing = c.u16()      # per-cell balancing bitmask
    warn1 = c.u8()
    warn2 = c.u8()

    problem_code = protect1 | ((protect2 & ~P2F_FULLY_CHARGED) << 8) | (fault << 16)
    warnings = (any(cell_warn) or any(temp_warn) or charge_current_warn
                or total_voltage_warn or discharge_current_warn or warn1 or warn2)

    return dict(
        pack_id=pack_id,
        problem_code=problem_code,
        problem=bool(problem_code) or bool(warnings),
        charge_mosfet=bool(system & SF_CHARGE_MOSFET_ON),
        discharge_mosfet=bool(system & SF_DISCHARGE_MOSFET_ON),
        charging=bool(system & SF_CHARGING),
        discharging=bool(system & SF_DISCHARGING),
        balancing=balancing,
    )


class PaceUart(BtBms):
    """PACE BMS over an RS232 / RS485 (USB-UART) adapter, ``paceic`` protocol."""

    # PACE serial links are 9600 8N1 (syssi docs + nkinnan default).
    BAUDRATE = 9600
    # paceic frames are ASCII-hex terminated by '\r' (0x0D) and never contain a
    # newline, so the generic readline() transport would block forever. Read one
    # '\r'-delimited frame at a time instead, with a short timeout so a silent
    # BMS doesn't wedge the reader thread.
    SERIAL_KWARGS = dict(eol=bytes([EOI]), timeout=2)

    VER = 0x25
    CID1 = 0x46
    ADR = 0x01       # pack address (header + payload target)
    TIMEOUT = 16
    _KEY = 0         # single outstanding request at a time (half-duplex)

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
            end = self._buffer.find(EOI)
            if end < 0:
                return  # frame still incomplete
            frame = bytes(self._buffer[:end + 1])
            del self._buffer[:end + 1]
            try:
                fields = parse_frame(frame)
            except ValueError as exc:
                self.logger.warning("discarding invalid paceic frame: %s", exc)
                continue
            if fields['cid2'] != CID2_OK:
                self.logger.warning("paceic error response, return-code 0x%02X", fields['cid2'])
            self._fetch_futures.set_result(self._KEY, fields)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("pace-uart", "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char  # the serial wrapper ignores the char on write

    async def disconnect(self):
        await self.stop_notify(self.UUID_RX)
        await super().disconnect()

    async def _read(self, cid2: int) -> dict:
        req = build_frame(self.VER, self.ADR, self.CID1, cid2, b'%02X' % self.ADR)
        with self._fetch_futures.acquire(self._KEY):
            await self.client.write_gatt_char(self.UUID_TX, data=req)
            return await self._fetch_futures.wait_for(self._KEY, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        analog = decode_analog((await self._read(CID2_READ_ANALOG))['info'])

        # Status is best-effort: it resolves current direction and alarms, but a
        # failure there must not lose the analog reading.
        status = None
        try:
            status = decode_status((await self._read(CID2_READ_STATUS))['info'])
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("paceic status read failed, using analog only: %s", exc)

        temps = analog['temps_c']
        # Layout: first 4 are cell probes, [4] is the MOSFET, [5:] ambient/env.
        mos_temperature = temps[4] if len(temps) > 4 else float('nan')
        temperatures = temps[:4] + temps[5:]

        # batmon convention: current > 0 == discharge (out of the pack). The
        # pack's own charging/discharging flags are authoritative for direction;
        # the analog current only supplies the magnitude. Fall back to the raw
        # signed analog value when the status frame is unavailable.
        current = analog['current_a']
        battery_charging = None
        if status is not None:
            mag = abs(analog['current_a'])
            if status['charging'] and not status['discharging']:
                current = -mag
            elif status['discharging'] and not status['charging']:
                current = mag
            battery_charging = status['charging']

        full_ah = analog['full_ah']
        design_ah = analog['design_ah']
        soc = (analog['remaining_ah'] / full_ah * 100) if full_ah > 0 else float('nan')
        soh = min(100.0, analog['full_ah'] / design_ah * 100) if design_ah > 0 else float('nan')

        self._last_cells = analog['cell_mv']
        self._last_temps = temperatures

        sample = BmsSample(
            voltage=analog['total_v'],
            current=current,
            charge=analog['remaining_ah'],
            capacity=design_ah,
            soc=float(soc),
            soh=float(soh),
            num_cycles=analog['cycles'],
            temperatures=temperatures,
            mos_temperature=mos_temperature,
            battery_charging=battery_charging,
            switches=(dict(charge=status['charge_mosfet'], discharge=status['discharge_mosfet'])
                      if status is not None else None),
            problem=status['problem'] if status is not None else None,
            problem_code=status['problem_code'] if status is not None else None,
        )
        return sample

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps


async def main():
    bms = PaceUart('serial', name='pace', adapter='/dev/ttyUSB0')
    await bms.connect()
    print(await bms.fetch())
    print(await bms.fetch_voltages())
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
