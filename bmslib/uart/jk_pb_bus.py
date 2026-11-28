"""Passive decoder for a JK-PB RS485 bus that already has a master on it.

On a JK-PB parallel group the master BMS (RS485 address 0) polls its slaves in a loop. With the
reference ``jk_pb_uart`` (batmon as a second, active master) the requests collide with the
master's own, the master never answers an outside request for address 0, and the 300-byte reply
frames carry no address, so frames of one pack end up in another one's sample.

The loop on the wire, as captured on a PB2A16S20P pair (master 0, slave 1), one cycle ~6 s::

    <15>.. 10 16 20 ..  <15>.. 10 16 1E ..      request for the last polled address, no reply
    55 AA EB 90 02 ...  55 AA EB 90 01 ...      the master's own status + settings frames,
                                                sent without a request frame in front
    <1> 10 16 20 ..     55 AA EB 90 02 ...      request, reply of the slave at that address
    <1> 10 16 1E ..     55 AA EB 90 01 ...
    <2> .. <15> ..                              requests for addresses nobody has: no reply

So the owner of a frame follows from where it sits in the loop: a frame of the type that the
last request asked for belongs to the addressed pack, any other frame is the master's. Nothing
is ever written to the bus.
"""
from typing import List, NamedTuple, Optional, Set, Tuple

from bmslib.uart.modbus_rtu import crc16_modbus

HEADER = b'\x55\xaa\xeb\x90'
FRAME_SIZE = 300
RESPONSE_TYPES = frozenset((0x01, 0x02, 0x03))

# FC16 write of one zero register: the registers that trigger a frame, and the frame type each yields.
REG_FRAME_TYPE = {0x161C: 0x03, 0x161E: 0x01, 0x1620: 0x02}

MASTER_ADDR = 0

_REQ_TAIL = b'\x00\x01\x02\x00\x00'  # reg count 1, 2 data bytes, value 0
_REQ_LEN = 4 + len(_REQ_TAIL) + 2  # 11
_ACK_LEN = 8  # addr 10 reg(2) count(2) crc(2)


class PbFrame(NamedTuple):
    owner: int  # RS485 address of the pack the frame belongs to
    ftype: int  # 0x01 settings, 0x02 status, 0x03 device info
    frame: bytes  # all 300 bytes, checksum verified
    solicited: bool  # True: answers the request just seen; False: sent unprompted (the master's own)


class PbBusParser:
    """Feed it raw bytes in chunks of any size; it returns the attributed frames."""

    def __init__(self):
        self._buf = bytearray()
        self._expect: Optional[Tuple[int, int]] = None  # (address, frame type) of the open request
        self._synced = False  # a request has been seen: the loop position is known
        self.polled: Set[int] = set()  # addresses seen in requests
        self.owners: Set[int] = set()  # addresses frames were attributed to
        self.requests = 0
        self.frames = 0
        self.corrupt = 0
        self.junk = 0  # bytes that were neither request, ack nor frame (the FC16 acks included)

    def reset(self):
        self._buf.clear()
        self._expect = None
        self._synced = False

    def summary(self) -> str:
        return 'requests for address(es) %s, frames for %s, %d corrupt, %d junk byte(s)' % (
            sorted(self.polled) or 'none', sorted(self.owners) or 'none', self.corrupt, self.junk)

    def feed(self, chunk: bytes) -> List[PbFrame]:
        buf = self._buf
        buf.extend(chunk)
        out: List[PbFrame] = []

        while buf:
            if buf[0] == 0x55:
                n = min(len(buf), len(HEADER))
                if bytes(buf[:n]) == HEADER[:n]:
                    if len(buf) < FRAME_SIZE:
                        break  # header-aligned, wait for the rest
                    frame = bytes(buf[:FRAME_SIZE])
                    if (sum(frame[:-1]) & 0xFF == frame[-1] and frame[4] in RESPONSE_TYPES
                            and frame.find(HEADER, 1) < 0):
                        del buf[:FRAME_SIZE]
                        self.frames += 1
                        attributed = self._attribute(frame)
                        if attributed:
                            out.append(attributed)
                        continue
                    # bad checksum, or the window runs into the next frame: resync on the next header
                    self.corrupt += 1
                    self._expect = None
                    del buf[:1]
                    continue

            verdict = self._modbus(buf)
            if verdict == 'wait':
                break
            if verdict == 'junk':
                self.junk += 1
                del buf[:1]
        return out

    def _modbus(self, buf: bytearray) -> str:
        """Try to consume a request or an ack at the start of ``buf``: 'ok', 'wait' or 'junk'."""
        if len(buf) >= 4 and not (buf[1] == 0x10 and (buf[2] << 8 | buf[3]) in REG_FRAME_TYPE):
            return 'junk'
        if len(buf) < 4:
            # could still become a request/ack once more bytes arrive
            return 'wait' if all(a == b for a, b in zip(buf[1:], b'\x10\x16')) else 'junk'

        if len(buf) >= _REQ_LEN and bytes(buf[4:9]) == _REQ_TAIL and \
                crc16_modbus(bytes(buf[:9])) == buf[9] | (buf[10] << 8):
            addr, reg = buf[0], buf[2] << 8 | buf[3]
            del buf[:_REQ_LEN]
            self.requests += 1
            self.polled.add(addr)
            self._expect = (addr, REG_FRAME_TYPE[reg])
            self._synced = True
            return 'ok'
        if len(buf) >= _ACK_LEN and bytes(buf[4:6]) == b'\x00\x01' and \
                crc16_modbus(bytes(buf[:6])) == buf[6] | (buf[7] << 8):
            del buf[:_ACK_LEN]
            return 'ok'
        if len(buf) < _REQ_LEN:
            n = min(len(buf), 4 + len(_REQ_TAIL))  # the crc bytes (9, 10) cannot be compared yet
            if bytes(buf[4:n]) == _REQ_TAIL[:n - 4]:
                return 'wait'  # still a possible request
        return 'junk'

    def _attribute(self, frame: bytes) -> Optional[PbFrame]:
        if not self._synced:
            return None  # joined mid-loop: no telling whose frame this is
        ftype = frame[4]
        expect, self._expect = self._expect, None  # a request answers at most one frame
        if expect is not None and expect[1] == ftype:
            owner, solicited = expect[0], True
        else:
            owner, solicited = MASTER_ADDR, False
        self.owners.add(owner)
        return PbFrame(owner, ftype, frame, solicited)
