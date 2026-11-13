"""Modbus RTU helpers shared by the wired drivers (JK-PB, Renogy, ...).

Frame layout (binary, CRC-16/MODBUS little-endian on the wire):

  request  FC 0x03/0x04:  <slave> <fc> <reg hi> <reg lo> <count hi> <count lo> <crc lo> <crc hi>
  response FC 0x03/0x04:  <slave> <fc> <bytecount> <data ...> <crc lo> <crc hi>
  exception:              <slave> <fc|0x80> <code> <crc lo> <crc hi>
"""
from typing import List, Optional, Tuple

FC_READ_COILS = 0x01
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_MULTIPLE = 0x10


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


def append_crc(body: bytes) -> bytes:
    crc = crc16_modbus(body)
    return bytes(body) + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def check_crc(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    return crc16_modbus(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


def build_read(slave: int, reg: int, count: int, fc: int = FC_READ_HOLDING) -> bytes:
    """Read-registers request (FC3 holding / FC4 input / FC1 coils)."""
    return append_crc(bytes([slave, fc, reg >> 8, reg & 0xFF, count >> 8, count & 0xFF]))


def build_write_multiple(slave: int, reg: int, values: bytes) -> bytes:
    """FC16 write-multiple request. ``values`` are the raw register bytes."""
    n_regs = len(values) // 2
    body = bytes([slave, FC_WRITE_MULTIPLE, reg >> 8, reg & 0xFF, n_regs >> 8, n_regs & 0xFF, len(values)]) + values
    return append_crc(body)


class ModbusError(ValueError):
    pass


def extract_read_frame(buf: bytearray, slave: Optional[int] = None) -> Optional[Tuple[int, int, bytes]]:
    """Pop the first complete read response (FC 1/3/4) or exception frame off ``buf``.

    Returns ``(slave, fc, data)`` or ``None`` while the frame is still incomplete.
    Raises :class:`ModbusError` for an exception reply. Bytes that cannot start a
    frame for ``slave`` (echo of our own request, other units' traffic, noise) are
    dropped one at a time until a CRC-valid frame lines up: a CRC-16 makes a false
    resync on random data ~1/65536, which is fine for a poll loop.
    """
    while True:
        if len(buf) < 5:
            return None
        if slave is not None and buf[0] != slave:
            del buf[0]
            continue
        fc = buf[1]
        if fc & 0x80:
            frame = bytes(buf[:5])
            if check_crc(frame):
                del buf[:5]
                raise ModbusError("modbus exception 0x%02x from slave 0x%02x (fc 0x%02x)"
                                  % (frame[2], frame[0], fc & 0x7F))
            del buf[0]
            continue
        if fc not in (FC_READ_COILS, FC_READ_HOLDING, FC_READ_INPUT):
            del buf[0]
            continue
        n = buf[2]
        total = 3 + n + 2
        if len(buf) < total:
            # Could also be junk that happens to promise a long frame; bound the
            # wait so a stray byte cannot stall framing forever.
            if len(buf) > 260:
                del buf[0]
                continue
            return None
        frame = bytes(buf[:total])
        if not check_crc(frame):
            del buf[0]
            continue
        del buf[:total]
        return frame[0], fc, frame[3:3 + n]


def u16_be(data: bytes, off: int, signed: bool = False) -> int:
    return int.from_bytes(data[off:off + 2], 'big', signed=signed)


def u32_be(data: bytes, off: int, signed: bool = False) -> int:
    return int.from_bytes(data[off:off + 4], 'big', signed=signed)


def regs_be(data: bytes) -> List[int]:
    return [u16_be(data, i) for i in range(0, len(data) - 1, 2)]
