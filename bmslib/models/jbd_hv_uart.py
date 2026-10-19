"""JBD / LWS high-voltage BMS ("HVBMS", up to 200 cells) over its wired port (#424).

Source: the vendor PC tool's syslog from the reporter's 96-cell pack
(2026-10-18), 9 request/reply pairs per command, kept as REAL_FRAMES in
bmslib/test/test_jbd_hv_uart.py. No protocol document. Only fields that
cross-check in those frames are decoded. The driver has not run against the
hardware, and the baud rate is unknown (the reporter's port sniffer log holds no
traffic): 9600 is a guess.

Requests are 4 bytes, `<addr> A1 <sel>`, sent exactly as the vendor tool does:

  01 A1 A5 B5   full view: up to 200 cells (the reporter: "A5 B5 = 200 cells")
  01 A1 A2 A3   short view: 30 cells, pack voltage, cell min/max

The tool finds packs by polling addresses 1..16 ("Batt Group"). Replies carry
no address, so packs sharing a bus are told apart only by taking turns.

Replies: `3F 55 <type> <data> <cksum> <type> 55 3F`, cksum = u16 BE sum of
every byte before it, from the leading 3F. Fixed lengths per type:

  type 0x85 (A5 B5), 530 bytes:
    3    cell mV, u16 BE x 220 slots (96 used here, then zeros)
    443  cell probe temperature, u8 - 40 C, x 58 (28 used; unused read 0x28 = 0 C)
    501  u16 x 7, not decoded: 0000, a value that moves with load (1914, 2042,
         765, ...; current?), 0x1088, 0000, 0x2710, 0000, 0x2710 (SOC/SOH 100.00?)
    515  two temperatures, u8 - 40 C (20 C, 36 C; probably ambient and MOS)
  type 0x68 (A2 A3), 133 bytes:
    3    cell mV, u16 BE x 30 slots (= the first 30 of the full view)
    63   4 probe temperatures (= the full view's first 4), then the two at 515
    79   a byte that moves with the full view's load value (current?), not decoded
    83   pack voltage, u16 BE, 0.1 V: 322.9 V where the 96 cells sum to 322.90 V
    89   max cell mV u16 + 1 byte, 92 min cell mV u16 + 1 byte (the byte is not
         the cell number)

Current and SOC are not published (NaN) until a capture with the tool's
displayed values pins down their offset, scale and sign.

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    jbd_hv_uart            # or jbd_hv_uart:2 for pack address 2

``jbd_hv_uart:<addr>:<baud>`` overrides the 9600 baud guess.
"""
import math
from typing import List

from bmslib.bms import BmsSample
from bmslib.bt import BtBms

HEAD = b'\x3f\x55'
TAIL = b'\x55\x3f'

CMD_FULL = b'\xa1\xa5\xb5'
CMD_SHORT = b'\xa1\xa2\xa3'

TYPE_FULL = 0x85
TYPE_SHORT = 0x68
FRAME_LEN = {TYPE_FULL: 530, TYPE_SHORT: 133}

FULL_CELLS, FULL_CELL_SLOTS = 3, 220
FULL_TEMPS, FULL_TEMP_SLOTS = 443, 58
FULL_EXTRA_TEMPS = 515
SHORT_VOLTAGE = 83

TEMP_OFFSET = 40
TEMP_UNUSED = 0x28  # what an unused probe slot reads: 0 C


def build_request(addr: int, cmd: bytes) -> bytes:
    return bytes([addr]) + cmd


def checksum(frame: bytes) -> int:
    return sum(frame[:-5]) & 0xFFFF


def check_frame(frame: bytes) -> int:
    """Validate a complete reply and return its type byte."""
    if frame[:2] != HEAD or frame[-2:] != TAIL:
        raise ValueError("jbd_hv frame: bad head/tail")
    t = frame[2]
    if FRAME_LEN.get(t) != len(frame):
        raise ValueError(f"jbd_hv frame: type 0x{t:02X} with length {len(frame)}")
    if frame[-3] != t:
        raise ValueError(f"jbd_hv frame: trailing type 0x{frame[-3]:02X} != 0x{t:02X}")
    cs = int.from_bytes(frame[-5:-3], 'big')
    if cs != checksum(frame):
        raise ValueError(f"jbd_hv frame: checksum 0x{cs:04X} != 0x{checksum(frame):04X}")
    return t


def _u16s(b: bytes, off: int, n: int) -> List[int]:
    return [int.from_bytes(b[off + 2 * i:off + 2 * i + 2], 'big') for i in range(n)]


def decode_full(frame: bytes) -> dict:
    slots = _u16s(frame, FULL_CELLS, FULL_CELL_SLOTS)
    n = max((i + 1 for i, v in enumerate(slots) if v), default=0)
    if not n:
        raise ValueError("jbd_hv: all cell voltages zero")
    cells = slots[:n]
    if not all(v < 5000 for v in cells):
        raise ValueError(f"jbd_hv: implausible cell voltage {max(cells)} mV")
    probes = frame[FULL_TEMPS:FULL_TEMPS + FULL_TEMP_SLOTS]
    # An unused slot reads 0 C. Trailing ones are unused; a real probe at
    # exactly 0 C at the end of the list is dropped with them (see fetch()).
    n_t = max((i + 1 for i, v in enumerate(probes) if v != TEMP_UNUSED), default=0)
    temps = [v - TEMP_OFFSET for v in probes[:n_t]]
    extra = [v - TEMP_OFFSET for v in frame[FULL_EXTRA_TEMPS:FULL_EXTRA_TEMPS + 2]]
    return dict(cell_mv=cells, temps=temps, extra_temps=extra)


def decode_short(frame: bytes) -> dict:
    return dict(voltage=int.from_bytes(frame[SHORT_VOLTAGE:SHORT_VOLTAGE + 2], 'big') / 10)


class JbdHvUart(BtBms):
    BAUDRATE = 9600  # unverified guess, see module docstring
    SERIAL_KWARGS = dict(eol=None, timeout=1)  # binary frames, may contain 0x0A
    TIMEOUT = 5  # the vendor tool gets its replies within ~1 s

    def __init__(self, address, **kwargs):
        spec = kwargs.pop('type_spec', None)
        parts = [p for p in (spec or '').split(':') if p]
        self.pack_addr = int(parts[0], 0) if parts else 1
        if len(parts) > 1:
            self.BAUDRATE = int(parts[1])
        if not 0 <= self.pack_addr <= 255:
            raise ValueError("jbd_hv pack address must be 0..255, got %r" % (self.pack_addr,))
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_cells: List[int] = []
        self._last_temps: List[float] = []
        self._n_temps = 0
        self._warned = set()

    def _notification_handler(self, sender, data):
        self._buffer += bytes(data)
        while True:
            start = self._buffer.find(HEAD)
            if start < 0:
                # keep a trailing 0x3F: it may be the first half of a header
                del self._buffer[:max(0, len(self._buffer) - 1)]
                return
            if start:
                del self._buffer[:start]  # also drops our own request, if echoed
            if len(self._buffer) < 3:
                return
            n = FRAME_LEN.get(self._buffer[2])
            if n is None:
                self.logger.warning("%s jbd_hv: unknown frame type 0x%02X, resyncing", self.name, self._buffer[2])
                del self._buffer[:2]
                continue
            if len(self._buffer) < n:
                return
            frame = bytes(self._buffer[:n])
            try:
                t = check_frame(frame)
            except ValueError as exc:
                self.logger.warning("%s discarding invalid jbd_hv frame: %s", self.name, exc)
                del self._buffer[:2]
                continue
            del self._buffer[:n]
            self._fetch_futures.set_result(t, frame)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("jbd-hv-uart-%d" % self.pack_addr, "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await super().disconnect()

    async def _q(self, cmd: bytes, reply_type: int) -> bytes:
        req = build_request(self.pack_addr, cmd)

        async def exchange():
            with self._fetch_futures.acquire(reply_type):
                await self.client.write_gatt_char(self.UUID_TX, data=req)
                return await self._fetch_futures.wait_for(reply_type, self.TIMEOUT)

        lock = getattr(self.client, 'bus_lock', None)
        if lock is None:
            return await exchange()
        async with lock():
            return await exchange()

    def _warn_once(self, key, msg, *args):
        if key not in self._warned:
            self._warned.add(key)
            self.logger.warning(msg, *args)

    async def fetch(self) -> BmsSample:
        full = await self._q(CMD_FULL, TYPE_FULL)
        f = decode_full(full)
        voltage = decode_short(await self._q(CMD_SHORT, TYPE_SHORT))['voltage']

        cell_sum = sum(f['cell_mv']) / 1000
        if abs(cell_sum - voltage) > max(1.0, 0.02 * voltage):
            self._warn_once('vsum', "%s jbd_hv: pack voltage %.1f V but cells sum to %.2f V",
                            self.name, voltage, cell_sum)

        # Undecoded fields, for the log until a capture identifies them.
        self.logger.debug("%s jbd_hv full[501:515]=%s", self.name, full[501:515].hex(' '))

        # Hold the probe count at its session maximum, so a last probe reading
        # exactly 0 C does not drop its topic.
        temps = f['temps']
        self._n_temps = max(self._n_temps, len(temps))
        temps = temps + [0.0] * (self._n_temps - len(temps))

        self._last_cells = f['cell_mv']
        # Which of the two extra probes is MOS is unknown, so both go in the list.
        self._last_temps = temps + f['extra_temps']
        return BmsSample(
            voltage=voltage,
            current=math.nan,  # not decoded yet (#424)
            temperatures=self._last_temps,
        )

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps
