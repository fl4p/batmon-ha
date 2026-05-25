"""JK / Jikong BMS UART (RS485) protocol — pure decoder + thin BtBms wrapper.

Distinct from the BLE protocol (``55 AA EB 90 …`` fixed 300-byte frames in
``jikong.py``). UART uses a TLV layout:

    4E 57 <len_hi> <len_lo>    Header + total frame size minus 2
    <4-byte addr>              BMS terminal number (always zero)
    <fn> <src> <type>          Function (0x06 read-all, 0x03 read-one),
                               source (0=BMS, 1=BLE, 2=GPS, 3=PC),
                               type (0=read, 1=reply, 2=BMS upload)
    79 2A <cells × 3 B>        Cell-voltage array (tag, byte-count, then
                               (index, V_hi, V_lo) triplets — 14 cells = 42 B)
    80 …  81 …  …              One register per tag (0x80..0xC0), widths in
                               REGISTER_WIDTHS below
    68 00 00                   End marker + 2 zero pad bytes
    <crc_hi> <crc_lo>          sum-mod-65536 over bytes[0..data_len), BE

Sources:
- syssi/esphome-jk-bms components/jk_modbus/jk_modbus.cpp (framing + CRC)
- syssi/esphome-jk-bms components/jk_bms/jk_bms.cpp (register byte map)
- jblance/mpp-solar mppsolar/protocols/jkserial.py (test response fixture)
- Louisvdw/dbus-serialbattery etc/dbus-serialbattery/bms/jkbms.py (sign
  convention on register 0x84 — bit 0x8000 = charging)
"""
from typing import Dict, Optional

from bmslib.bms import BmsSample

HEADER = b"\x4e\x57"

# Sum-mod-65536, big-endian
def crc16_jk(data: bytes) -> int:
    return sum(data) & 0xFFFF


# Register tag → value byte count. ``-1`` means variable length (the next byte
# in the stream is the count). Mirrors the offsets that syssi's parser uses.
REGISTER_WIDTHS: Dict[int, int] = {
    0x79: -1,  # cell array, length byte follows
    # status / live readings
    0x80: 2, 0x81: 2, 0x82: 2, 0x83: 2, 0x84: 2, 0x85: 1, 0x86: 1, 0x87: 2,
    0x89: 4, 0x8A: 2, 0x8B: 2, 0x8C: 2,
    # protection / setting registers (read-only here)
    0x8E: 2, 0x8F: 2, 0x90: 2, 0x91: 2, 0x92: 2, 0x93: 2, 0x94: 2, 0x95: 2,
    0x96: 2, 0x97: 2, 0x98: 2, 0x99: 2, 0x9A: 2, 0x9B: 2, 0x9C: 2,
    0x9D: 1, 0x9E: 2, 0x9F: 2,
    0xA0: 2, 0xA1: 2, 0xA2: 2, 0xA3: 2, 0xA4: 2, 0xA5: 2, 0xA6: 2, 0xA7: 2, 0xA8: 2,
    0xA9: 1, 0xAA: 4, 0xAB: 1, 0xAC: 1, 0xAD: 2, 0xAE: 1, 0xAF: 1,
    # device / version strings
    0xB0: 2, 0xB1: 1, 0xB2: 10, 0xB3: 1, 0xB4: 8, 0xB5: 4, 0xB6: 4, 0xB7: 15,
    0xB8: 1, 0xB9: 4, 0xBA: 24,
    0xC0: 1,
}


class JKUartFrameError(Exception):
    pass


def build_status_request() -> bytes:
    """Builds the 21-byte ``read all registers`` request frame.

    Returns the exact byte string ``4E 57 00 13 00 00 00 00 06 03 00 00 00 00
    00 00 68 00 00 01 29`` documented in syssi's jk_modbus.cpp:read_registers.
    """
    body = bytes([
        0x4E, 0x57,  # header
        0x00, 0x13,  # data_len = 19
        0x00, 0x00, 0x00, 0x00,  # bms terminal addr
        0x06,        # function: read-all
        0x03,        # source: computer
        0x00,        # type: read
        0x00,        # register address = 0 (read-all)
        0x00, 0x00, 0x00, 0x00,  # record number
        0x68,        # end marker
        0x00, 0x00,  # crc unused
    ])
    crc = crc16_jk(body)
    return body + crc.to_bytes(2, "big")


def validate_frame(buf: bytes) -> int:
    """Validate header / length / CRC. Returns ``data_len`` (frame body extent,
    excluding the trailing 2 CRC bytes). Raises on invalid frame."""
    if len(buf) < 13:
        raise JKUartFrameError(f"frame too short: {len(buf)} bytes")
    if buf[0:2] != HEADER:
        raise JKUartFrameError(f"bad header: {buf[0:2].hex()}")
    data_len = int.from_bytes(buf[2:4], "big")
    if len(buf) < data_len + 2:
        raise JKUartFrameError(
            f"frame truncated: have {len(buf)}, need {data_len + 2}"
        )
    crc_calc = crc16_jk(buf[:data_len])
    crc_recv = int.from_bytes(buf[data_len:data_len + 2], "big")
    if crc_calc != crc_recv:
        raise JKUartFrameError(
            f"crc mismatch: calc=0x{crc_calc:04x} recv=0x{crc_recv:04x}"
        )
    return data_len


def parse_tlv_body(body: bytes) -> dict:
    """Walk the TLV body and return ``{tag: value_bytes}``.

    ``body`` is everything after the 11-byte header and up to (not including)
    the trailing ``68 00 00 <crc>``. The 0x79 cell-array is normalised to
    ``{0x79: [(idx, mv), ...]}``.
    """
    out: dict = {}
    i = 0
    while i < len(body):
        tag = body[i]
        if tag == 0x68:
            break
        if tag == 0x00:
            # 4-byte "record number" padding that appears after the last
            # tagged register and before the 0x68 end-marker. syssi's parser
            # ignores it by reading fields at fixed offsets; we skip it here.
            i += 1
            continue
        width = REGISTER_WIDTHS.get(tag)
        if width is None:
            raise JKUartFrameError(
                f"unknown register tag 0x{tag:02x} at offset {i}"
            )
        i += 1
        if width == -1:
            # 0x79 cell array: next byte is total payload size in bytes
            n = body[i]
            i += 1
            if n % 3 != 0:
                raise JKUartFrameError(
                    f"cell-array length {n} not divisible by 3"
                )
            cells = []
            for j in range(0, n, 3):
                idx = body[i + j]
                mv = int.from_bytes(body[i + j + 1: i + j + 3], "big")
                cells.append((idx, mv))
            out[tag] = cells
            i += n
        else:
            out[tag] = body[i:i + width]
            i += width
    return out


def _i16_be(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=True)


def _u16_be(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=False)


def _u32_be(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=False)


def _decode_temp(raw: int) -> float:
    """JK UART temperature encoding (syssi's ``get_temperature_`` in
    ``components/jk_bms/jk_bms.h``): raw 0..99 are positive Celsius;
    raw > 99 means subzero, decoded as ``99 - raw`` (so raw 100 → −1 °C,
    raw 140 → −41 °C).

    Note: the inline comment in ``jk_bms.cpp`` claims "100 = 100°C" but the
    actual ``get_temperature_`` implementation disagrees — the comment is
    documentation of the field's *semantic intent*, not what the code does.
    """
    return float(99 - raw) if raw > 99 else float(raw)


def _decode_current_0x84(raw: int) -> float:
    """Bit 0x8000 is the charge/discharge flag.
    raw < 32768  → discharging, magnitude = raw / 100,   return negative
    raw >= 32768 → charging,    magnitude = (raw-32768)/100, return positive
    Matches batmon-ha's convention: negative = discharging.
    """
    if raw >= 0x8000:
        return (raw - 0x8000) / 100.0
    return -raw / 100.0


def parse_status_frame(buf: bytes) -> BmsSample:
    """Parse a full ``4E 57 …`` UART response into a :class:`BmsSample`.

    Raises :class:`JKUartFrameError` on header / length / CRC issues. Missing
    registers default to ``None`` / NaN so partial frames still decode.
    """
    data_len = validate_frame(buf)
    # 11-byte fixed header, body ends before the trailing `68 00 00 crc_hi crc_lo`.
    # In all observed frames the 0x68 sits exactly 3 bytes before the CRC.
    body = bytes(buf[11:data_len - 3])
    fields = parse_tlv_body(body)

    cells = fields.get(0x79, [])
    cell_voltages_mv = [mv for _, mv in cells]

    voltage = _u16_be(fields[0x83]) / 100.0 if 0x83 in fields else None
    current = _decode_current_0x84(_u16_be(fields[0x84])) if 0x84 in fields else None

    temperatures = []
    if 0x81 in fields:
        temperatures.append(_decode_temp(_u16_be(fields[0x81])))
    if 0x82 in fields:
        temperatures.append(_decode_temp(_u16_be(fields[0x82])))

    mos_temperature = (
        _decode_temp(_u16_be(fields[0x80])) if 0x80 in fields else None
    )

    soc = float(fields[0x85][0]) if 0x85 in fields else None
    num_cycles = _u16_be(fields[0x87]) if 0x87 in fields else None
    total_charge_throughput = (
        _u32_be(fields[0x89]) if 0x89 in fields else None
    )
    num_cells = _u16_be(fields[0x8A]) if 0x8A in fields else None
    # capacity is in register 0xAA (4 bytes, integer Ah) per syssi
    capacity = float(_u32_be(fields[0xAA])) if 0xAA in fields else None

    # Operation-mode bitmask at 0x8C: bit0=charge, bit1=discharge, bit2=balance
    switches = None
    if 0x8C in fields:
        mode = _u16_be(fields[0x8C])
        switches = dict(
            charge=bool(mode & 0x0001),
            discharge=bool(mode & 0x0002),
            balance=bool(mode & 0x0004),
        )

    # Charging cap is total_capacity × soc/100 — let BmsSample re-derive charge
    # only when soc is reported as a fraction; the JK reports an integer SOC
    # against its internal nominal, same as BLE.
    charge = None
    if capacity is not None and soc is not None:
        charge = capacity * soc / 100.0

    import math
    return BmsSample(
        voltage=voltage if voltage is not None else math.nan,
        current=current if current is not None else math.nan,
        soc=soc if soc is not None else math.nan,
        charge=charge if charge is not None else math.nan,
        capacity=capacity if capacity is not None else math.nan,
        num_cycles=num_cycles if num_cycles is not None else math.nan,
        total_charge_throughput=(
            float(total_charge_throughput) if total_charge_throughput is not None else math.nan
        ),
        temperatures=temperatures or None,
        mos_temperature=mos_temperature if mos_temperature is not None else math.nan,
        switches=switches,
    )


def feed_buffer(buf: bytearray, chunk: bytes) -> Optional[bytes]:
    """Accumulate ``chunk`` into ``buf`` and return the first complete frame
    found (and drop it from ``buf``), or ``None`` if no full frame is buffered.

    Handles two common UART symptoms:
      - the serial wrapper delivers data in arbitrary chunks (``readline``
        splits on any 0x0A byte that appears mid-payload), so we must wait
        until ``data_len + 2`` bytes are available before parsing;
      - junk before the first ``4E 57`` header is skipped silently.
    """
    buf.extend(chunk)
    # Discard bytes until the header
    while len(buf) >= 2 and bytes(buf[0:2]) != HEADER:
        del buf[0]
    if len(buf) < 4:
        return None
    data_len = int.from_bytes(buf[2:4], "big")
    total = data_len + 2
    if len(buf) < total:
        return None
    frame = bytes(buf[:total])
    del buf[:total]
    return frame


def parse_device_info(buf: bytes) -> dict:
    """Extract the manufacturer/version strings from registers 0xB4/0xB7/0xBA."""
    validate_frame(buf)
    data_len = int.from_bytes(buf[2:4], "big")
    fields = parse_tlv_body(bytes(buf[11:data_len - 3]))

    def _ascii(b: Optional[bytes]) -> Optional[str]:
        if b is None:
            return None
        return b.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    return dict(
        device_id=_ascii(fields.get(0xB4)),
        production_date=_ascii(fields.get(0xB5)),
        software_version=_ascii(fields.get(0xB7)),
        manufacturer=_ascii(fields.get(0xBA)),
        protocol_version=fields[0xC0][0] if 0xC0 in fields else None,
    )


# ---------------------------------------------------------------------------
# Thin BtBms wrapper — re-uses the existing SerialBleakClientWrapper transport
# (bmslib/wired/__init__.py) so users can select this BMS via
# ``address: serial`` + ``adapter: /dev/ttyUSB0`` in options.json.

import asyncio
from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms


class JKUart(BtBms):
    """JK BMS over RS485 / UART.

    Uses the same ``SerialBleakClientWrapper`` transport plumbing as the BLE
    JK class but implements the genuine UART TLV protocol (``4E 57 …``)
    rather than the BLE protocol that the wrapper formerly assumed.

    Configure with:
        - address: serial
        - adapter: /dev/ttyUSB0  (or ``serial:/dev/ttyUSB0``)
        - alias:   any human-readable name
    """

    TIMEOUT = 8

    def __init__(self, address, keep_alive=True, **kwargs):
        super().__init__(address, keep_alive=keep_alive, **kwargs)
        self._buffer = bytearray()
        self._last_frame: Optional[bytes] = None
        self._last_frame_event = asyncio.Event()

    def _notification_handler(self, _sender, data):
        frame = feed_buffer(self._buffer, bytes(data))
        if frame is None:
            return
        try:
            validate_frame(frame)
        except JKUartFrameError as e:
            self.logger.warning("JK UART bad frame: %s", e)
            return
        self._last_frame = frame
        self._last_frame_event.set()

    async def connect(self, timeout=20):
        await super().connect(timeout=timeout)
        # The SerialBleakClientWrapper ignores the char argument and just
        # forwards every byte it reads to all registered callbacks, so a
        # stub char is fine here.
        from bmslib.wired import SerialCharStub
        await self.client.start_notify(SerialCharStub("uart", "notify"), self._notification_handler)

    async def disconnect(self):
        try:
            from bmslib.wired import SerialCharStub
            await self.client.stop_notify(SerialCharStub("uart", "notify"))
        except Exception:
            pass
        await super().disconnect()

    async def _send_and_wait(self) -> bytes:
        """Send the read-all request, wait for one complete response frame."""
        self._last_frame = None
        self._last_frame_event.clear()
        from bmslib.wired import SerialCharStub
        await self.client.write_gatt_char(SerialCharStub("uart", "write"), data=build_status_request())
        await asyncio.wait_for(self._last_frame_event.wait(), timeout=self.TIMEOUT)
        assert self._last_frame is not None
        return self._last_frame

    async def fetch(self) -> BmsSample:
        frame = await self._send_and_wait()
        return parse_status_frame(frame)

    async def fetch_device_info(self) -> DeviceInfo:
        # JK UART carries everything in one response; reuse the last frame
        # if we have it, otherwise pull a fresh one.
        frame = self._last_frame or await self._send_and_wait()
        info = parse_device_info(frame)
        return DeviceInfo(
            mnf="JK",
            model=info.get("manufacturer") or "JK BMS",
            hw_version=None,
            sw_version=info.get("software_version"),
            name=info.get("device_id"),
            sn=info.get("production_date"),
        )

    async def fetch_voltages(self):
        if self._last_frame is None:
            await self._send_and_wait()
        data_len = validate_frame(self._last_frame)
        body = self._last_frame[11:data_len - 3]
        fields = parse_tlv_body(body)
        return [mv for _, mv in fields.get(0x79, [])]
