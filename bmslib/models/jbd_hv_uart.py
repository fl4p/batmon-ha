"""JBD / LWS high-voltage BMS ("HVBMS", up to 200 cells) over RS485 (#424).

Sources, all from the reporter's 96-cell pack (#424):
  - the vendor PC tool's syslogs (2026-10-18 idle-ish, 2026-10-19 discharging
    ~3.5 A, idle, charging ~9.1 A): raw request/reply pairs, kept as
    REAL_FRAMES in bmslib/test/test_jbd_hv_uart.py;
  - the tool's own data exports of the 2026-10-19 runs (xlsx), which label the
    fields: current, SOC, total voltage, P+/P- voltage, insulation resistance,
    ambient and MOS temperature. Every field decoded here matches them.
  - port settings from the reporter: 9600 8N1, RS485 (FT232 adapter).
No protocol document covers these frames. The "JBD HVV1 Lithium Battery Modbus
Communication Protocol" V1.36 he got from the supplier is Modbus RTU (function
0x04, slave address 51..65), not what the vendor tool speaks; whether the BMS
also answers it is untested. The driver has not run against the hardware.

Requests are 4 bytes, `<addr> A1 <sel>`, sent exactly as the vendor tool does:

  01 A1 A5 B5   full view: up to 200 cells, all probes, current in mA
  01 A1 A2 A3   short view: 30 cells, SOC, pack voltage, cell min/max

The tool finds packs by polling addresses 1..16 ("Batt Group"). Replies carry
no address, so packs sharing a bus are told apart only by taking turns.

Replies: `3F 55 <type> <data> <cksum> <type> 55 3F`, cksum = u16 BE sum of
every byte before it, from the leading 3F. Fixed lengths per type:

  type 0x85 (A5 B5), 530 bytes:
    3    cell mV, u16 BE x 220 slots (96 used here, then zeros)
    443  cell probe temperature, u8 - 40 C, x 58 (28 used; unused read 0x28 = 0 C)
    501  current, s32 BE, mA, charge positive (+9189 charging, -3573 discharging)
    505  P+/P- (output terminal) voltage, u16 BE, 0.1 V
    507  insulation resistance B+~PE, then B-~PE, u32 BE, kOhm
    515  ambient temperature, then MOS temperature, u8 - 40 C
  type 0x68 (A2 A3), 133 bytes:
    3    cell mV, u16 BE x 30 slots (= the first 30 of the full view)
    63   4 probe temperatures (= the full view's first 4), MOS, ambient
    78   current, 0.1 A, sign-magnitude (bit 15 set = discharge: 0x801C = -2.8 A)
    80   SOC, u16 BE, 0.1 %
    83   pack voltage, u16 BE, 0.1 V: 322.9 V where the 96 cells sum to 322.90 V
    89   max cell mV u16 + 1 byte, 92 min cell mV u16 + 1 byte (the byte is not
         the cell number)

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    jbd_hv_uart            # or jbd_hv_uart:2 for pack address 2

``jbd_hv_uart:<addr>:<baud>`` overrides the 9600 baud default.
"""
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
FULL_CURRENT = 501
FULL_OUTPUT_VOLTAGE = 505
FULL_INSULATION = 507
FULL_AMBIENT, FULL_MOS = 515, 516
SHORT_CURRENT = 78
SHORT_SOC = 80
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
    return dict(
        cell_mv=cells, temps=temps,
        current=int.from_bytes(frame[FULL_CURRENT:FULL_CURRENT + 4], 'big', signed=True) / 1000,
        output_voltage=int.from_bytes(frame[FULL_OUTPUT_VOLTAGE:FULL_OUTPUT_VOLTAGE + 2], 'big') / 10,
        insulation_pos=int.from_bytes(frame[FULL_INSULATION:FULL_INSULATION + 4], 'big'),
        insulation_neg=int.from_bytes(frame[FULL_INSULATION + 4:FULL_INSULATION + 8], 'big'),
        ambient=frame[FULL_AMBIENT] - TEMP_OFFSET,
        mos=frame[FULL_MOS] - TEMP_OFFSET,
    )


def decode_short(frame: bytes) -> dict:
    cur = int.from_bytes(frame[SHORT_CURRENT:SHORT_CURRENT + 2], 'big')
    soc = int.from_bytes(frame[SHORT_SOC:SHORT_SOC + 2], 'big') / 10
    if soc > 100:
        raise ValueError(f"jbd_hv: implausible SOC {soc}")
    return dict(
        voltage=int.from_bytes(frame[SHORT_VOLTAGE:SHORT_VOLTAGE + 2], 'big') / 10,
        soc=soc,
        current=(-(cur & 0x7FFF) if cur & 0x8000 else cur) / 10,
    )


class JbdHvUart(BtBms):
    BAUDRATE = 9600  # 8N1, per the reporter and the supplier's Modbus document
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
        self._sign_mismatches = 0
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
                # Half duplex: anything still buffered is a stale or broken
                # reply. A false 3F 55 85 in it would hold back the next
                # short reply until it had collected 530 bytes.
                self._buffer.clear()
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
        f = decode_full(await self._q(CMD_FULL, TYPE_FULL))
        short = decode_short(await self._q(CMD_SHORT, TYPE_SHORT))
        voltage = short['voltage']

        cell_sum = sum(f['cell_mv']) / 1000
        if abs(cell_sum - voltage) > max(1.0, 0.02 * voltage):
            self._warn_once('vsum', "%s jbd_hv: pack voltage %.1f V but cells sum to %.2f V",
                            self.name, voltage, cell_sum)
        # Both views carry the current, read about a second apart, so a load
        # step can flip one sign once; only a lasting disagreement is reported.
        if abs(f['current']) > 1 and abs(short['current']) > 1 and (f['current'] > 0) != (short['current'] > 0):
            self._sign_mismatches += 1
            if self._sign_mismatches == 3:
                self.logger.warning("%s jbd_hv: current %.2f A (full view) and %.1f A (short view) disagree in sign "
                                    "in 3 samples running", self.name, f['current'], short['current'])
        else:
            self._sign_mismatches = 0

        # Hold the probe count at its session maximum, so a last probe reading
        # exactly 0 C does not drop its topic.
        temps = f['temps']
        self._n_temps = max(self._n_temps, len(temps))
        temps = temps + [0.0] * (self._n_temps - len(temps))

        self._last_cells = f['cell_mv']
        self._last_temps = [f['ambient']] + temps
        return BmsSample(
            voltage=voltage,
            current=-f['current'],  # BMS: charge positive; batmon: discharge positive
            soc=short['soc'],
            temperatures=self._last_temps,
            mos_temperature=f['mos'],
            extra_values={
                'jbd_hv/output_voltage': f['output_voltage'],
                'jbd_hv/insulation_pos': f['insulation_pos'],
                'jbd_hv/insulation_neg': f['insulation_neg'],
            },
            extra_desc={
                'jbd_hv/output_voltage': {"name": "P+/P- Voltage", "device_class": "voltage", "unit": "V",
                                          "state_class": "measurement"},
                'jbd_hv/insulation_pos': {"name": "Insulation Resistance B+ to PE", "unit": "kΩ",
                                          "state_class": "measurement", "icon": "omega"},
                'jbd_hv/insulation_neg': {"name": "Insulation Resistance B- to PE", "unit": "kΩ",
                                          "state_class": "measurement", "icon": "omega"},
            },
        )

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps
