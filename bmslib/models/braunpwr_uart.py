"""BraunPWR / KS48100-HO20_V1 rack BMS over its wired UART (#403).

UNTESTED ON HARDWARE. Built from two sources only, and no raw reply frame has
been captured yet:
  - the working ESPHome config of the #403 reporter (bialy39), who pulled the
    BMS's Quectel FC41D WiFi/BLE module (its BLE link kept dropping) and polls
    the BMS from an ESP32 through the ``uartex`` component instead;
  - the manufacturer's protocol PDF "X 1363_Protocol_V1_0_1" (Chinese), which is
    YD/T 1363.3-2014: table 23 (42H field order), table 24 (status words),
    table 19 (current sign).
Where the two meet they agree (SOI 0x3E, 42H echo in the response's CID1 slot,
field offsets up to the alarm words). Wiring is unconfirmed: the reporter's
ESP32 sits either on the header the FC41D module plugged into or behind an
RS485 transceiver; we don't know which.

Wire format: the ASCII-hex YD/T 1363 framing already implemented in
``bmslib.models.pace`` (builder, parser, LENGTH/CHKSUM), with a different SOI:

  request   '>' 22 <ADR> 4A 42 E002 "01" <CHKSUM> '\\r'   9600 8N1
  response  '>' 22 <ADR> 42 <RTN> <LENGTH> <DATAINFO> <CHKSUM> '\\r'

ADR is the pack address. It is set with the DIP switch, but how the switch maps
to ADR is unverified; the reporter finds it by sweeping 1..16 and taking the one
that answers. INFO "01" is COMMAND GROUP 1 (PDF: group 01..nn = one battery
group, table 23 layout; FF would prepend a group count), sent as-is whatever
the ADR, as the reporter does. The response puts the 42H command echo where
a request has CID1 and the return code RTN (00 = OK) where a request has CID2.

DATAINFO of the 42H (fixed-point) response, PDF table 23, as parsed here:
  DATAFLAG u8 | SOC 0.01 % u16 | pack 0.01 V u16 | m u8 | cell mV u16 x m |
  ambient, pack, MOS temperature 0.1 C s16 x 3 | n u8 | cell temp 0.1 C s16 x n |
  current s16 (charge positive) | internal resistance u16 | SOH % u16 |
  flag u8 (bit0: current unit 0.01 A, else 0.1 A) | full-charge 0.01 Ah u16 |
  remaining 0.01 Ah u16 | cycles u16 | voltage, current, temperature, alarm,
  FET status u16 x 5 | cell OVP, UVP, high-V, low-V alarm (1-16) u16 x 4 |
  balancing (1-16), (17-32) u16 x 2 | ... (per-cell 17-32 words, state
  machine, I/O bits: not read)
Everything after the current is optional here: the reporter only relies on
fields up to the current (plus the status words for his alarm text).

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    braunpwr_uart          # or braunpwr_uart:2 for pack address 2
    alias:   any human-readable name

``braunpwr_uart:<addr>:<baud>`` overrides the 9600 baud default.
"""
import math
from typing import List, Optional

from bmslib.bms import BmsSample
from bmslib.bt import BtBms
from bmslib.models.pace import EOI, build_frame, parse_frame, _Cursor, CID2_OK

VER = 0x22
CID1 = 0x4A        # LiFePO4 BMS device type (PDF section 9)
CID2_ANALOG = 0x42  # analog values, fixed point (PDF 10.2.2)
COMMAND_GROUP = 0x01

# Status word bits (PDF table 24). Bits that describe state rather than a fault
# are kept out of problem_code, or every normal charge/discharge would "alarm".
VS_FULL_CHARGE = 1 << 15       # voltage status: full-charge protection (end of charge)
CS_CHARGING = 1 << 0           # current status: charging
CS_CURRENT_X10 = 1 << 10       # current status: 1 = value is actual x10, 0 = x100
CS_PROTECTION = 0x03FC         # current status B2..B9: over-current/short/reverse
AS_RESERVED = 1 << 0           # alarm status B0 is reserved
FET_CHARGE_ON = 1 << 0
FET_DISCHARGE_ON = 1 << 1


def build_request(adr: int, soi: int = 0x3E) -> bytes:
    return build_frame(VER, adr, CID1, CID2_ANALOG, b'%02X' % COMMAND_GROUP, soi=soi)


def _current_scale(flag: Optional[int], cur_status: Optional[int]) -> tuple:
    """A/count of the raw current, and whether the frame's two unit indicators
    contradict each other.

    The PDF declares the unit twice (table 23 flag bit0, table 24 current status
    B10); the reporter's working config ignores both and uses 0.01 A. We only
    switch to 0.1 A when every indicator present in the frame says so, i.e. we
    take the field-tested value unless the BMS says otherwise consistently.
    """
    votes = []
    if flag is not None:
        votes.append(0.01 if flag & 1 else 0.1)
    if cur_status is not None:
        votes.append(0.1 if cur_status & CS_CURRENT_X10 else 0.01)
    if votes and all(v == 0.1 for v in votes):
        return 0.1, False
    return 0.01, len(set(votes)) > 1


def decode_analog(info: bytes) -> dict:
    c = _Cursor(info)
    c.u8()  # DATAFLAG
    soc = c.u16() / 100
    voltage = c.u16() / 100
    m = c.u8()
    if not 1 <= m <= 32:
        raise ValueError(f"braunpwr analog: implausible cell count {m}")
    cell_mv = [c.u16() for _ in range(m)]
    t_ambient, t_pack, t_mos = (c.s16() / 10 for _ in range(3))
    n = c.u8()
    if not 0 <= n <= 16:
        raise ValueError(f"braunpwr analog: implausible temperature count {n}")
    cell_temps = [c.s16() / 10 for _ in range(n)]
    current_raw = c.s16()
    if sum(cell_mv) == 0:
        raise ValueError("braunpwr analog: all cell voltages zero")
    if soc > 100:
        raise ValueError(f"braunpwr analog: implausible SOC {soc}")

    d = dict(soc=soc, voltage=voltage, cell_mv=cell_mv, t_ambient=t_ambient, t_pack=t_pack, t_mos=t_mos,
             cell_temps=cell_temps, current_raw=current_raw, soh=math.nan, flag=None, full_ah=math.nan,
             remaining_ah=math.nan, cycles=math.nan, status=None, balancing=None)

    # Optional tail. Each block is all-or-nothing: a block that starts but
    # ends early raises in _Cursor rather than filling the rest with zeros.
    if not c.at_end():
        c.u16()  # internal resistance, unit undocumented
        d['soh'] = c.u16()
        d['flag'] = c.u8()
    if not c.at_end():
        d['full_ah'] = c.u16() / 100
        d['remaining_ah'] = c.u16() / 100
        d['cycles'] = c.u16()
        d['status'] = dict(voltage=c.u16(), current=c.u16(), temperature=c.u16(), alarm=c.u16(), fet=c.u16())
    if not c.at_end():
        for _ in range(4):
            c.u16()  # per-cell OVP/UVP/high/low (1-16): already summarised in the voltage status word
        d['balancing'] = c.u16() | (c.u16() << 16)

    scale, conflict = _current_scale(d['flag'], d['status'] and d['status']['current'])
    d['current_scale'] = scale
    d['current_unit_conflict'] = conflict
    d['current'] = current_raw * scale
    return d


def problem_code(status: dict) -> int:
    """Fault bits of the five status words packed into 61 bits (fits the signed
    64-bit problem_code column): voltage status 0-15 (without end-of-charge),
    current-status protections 16-23, temperature status 24-39, alarm status
    40-55 (without the reserved bit), FET-status faults 56-60."""
    fet = status['fet']
    # B2/B3 FET damaged, B13/B14 AFE faults, B15 low-battery protection
    fet_faults = (((fet >> 2) & 0b11) | (((fet >> 13) & 0b111) << 2))
    return ((status['voltage'] & ~VS_FULL_CHARGE & 0xFFFF)
            | (((status['current'] & CS_PROTECTION) >> 2) << 16)
            | (status['temperature'] << 24)
            | ((status['alarm'] & ~AS_RESERVED & 0xFFFF) << 40)
            | (fet_faults << 56))


class BraunPwrUart(BtBms):
    BAUDRATE = 9600
    SERIAL_KWARGS = dict(eol=bytes([EOI]), timeout=2)
    TIMEOUT = 5  # the PDF gives the BMS 500 ms to answer
    # The PDF (table 3, section 8.1) and the reporter's working config both use
    # '>' (0x3E). The '~' frames captured from this BMS on RS485A were its
    # Pylontech mode (VER 20, CID1 46), a different INFO layout: decoding one
    # here would publish plausible nonsense, so '~' is rejected, not accepted.
    SOI = 0x3E
    RX_SOIS = (SOI,)
    _KEY = 'braunpwr'

    def __init__(self, address, **kwargs):
        spec = kwargs.pop('type_spec', None)
        parts = [p for p in (spec or '').split(':') if p]
        self.pack_addr = int(parts[0], 0) if parts else 1
        if len(parts) > 1:
            self.BAUDRATE = int(parts[1])
        if not 1 <= self.pack_addr <= 254:
            raise ValueError("braunpwr pack address must be 1..254, got %r" % (self.pack_addr,))
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_req = b''
        self._last_cells: List[int] = []
        self._last_temps: List[float] = []
        self._warned = set()

    def _warn_once(self, key, msg, *args):
        if key not in self._warned:
            self._warned.add(key)
            self.logger.warning(msg, *args)

    def _notification_handler(self, sender, data):
        self._buffer += bytes(data)
        while True:
            starts = [i for i in (self._buffer.find(s) for s in self.RX_SOIS) if i >= 0]
            if not starts:
                self._buffer.clear()
                return
            start = min(starts)
            if start:
                del self._buffer[:start]
            end = self._buffer.find(EOI)
            if end < 0:
                return
            frame = bytes(self._buffer[:end + 1])
            del self._buffer[:end + 1]
            if frame == self._last_req:
                continue  # our own request, echoed by an adapter that hears its TX
            try:
                fields = parse_frame(frame, sois=self.RX_SOIS)
            except ValueError as exc:
                self.logger.warning("%s discarding invalid braunpwr frame: %s", self.name, exc)
                continue
            if fields['adr'] != self.pack_addr:
                self.logger.debug("%s ignoring frame from address %d", self.name, fields['adr'])
                continue  # another pack on the bus
            self._fetch_futures.set_result(self._KEY, fields)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("braunpwr-uart-%d" % self.pack_addr, "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await super().disconnect()

    async def _read(self) -> dict:
        req = build_request(self.pack_addr, soi=self.SOI)
        self._last_req = req

        async def exchange():
            with self._fetch_futures.acquire(self._KEY):
                await self.client.write_gatt_char(self.UUID_TX, data=req)
                return await self._fetch_futures.wait_for(self._KEY, self.TIMEOUT)

        lock = getattr(self.client, 'bus_lock', None)
        fields = await exchange() if lock is None else await _locked(lock, exchange)
        return self._check_response(fields)

    def _check_response(self, fields: dict) -> dict:
        if fields['cid2'] != CID2_OK:
            raise ValueError(f"braunpwr returned error code 0x{fields['cid2']:02X} (RTN) for request 0x42")
        if fields['cid1'] != CID2_ANALOG:
            # PDF table 26 and the reporter's config both have the 42H echo here.
            # Not fatal: checksum, LENGTH, RTN and ADR already vouch for the frame.
            self._warn_once('cid1', "%s braunpwr response carries 0x%02X where 0x42 was expected, decoding anyway",
                            self.name, fields['cid1'])
        return fields

    async def fetch(self) -> BmsSample:
        a = decode_analog((await self._read())['info'])
        if a['current_unit_conflict']:
            self._warn_once('unit', "%s braunpwr frame disagrees with itself on the current unit "
                                    "(flag 0x%02X, current status 0x%04X), assuming 0.01 A",
                            self.name, a['flag'], a['status']['current'])
        elif a['current_scale'] != 0.01:
            self._warn_once('unit', "%s braunpwr reports its current in 0.1 A units", self.name)

        s = a['status']
        self._last_cells = a['cell_mv']
        # PDF order: ambient, pack, then the n cell probes. MOS is separate.
        self._last_temps = [a['t_ambient'], a['t_pack']] + a['cell_temps']
        full_ah = a['full_ah']
        return BmsSample(
            voltage=a['voltage'],
            # PDF table 19: current signed, charging positive; the reporter's
            # config agrees (charging when the raw value is > 0.5 A). batmon
            # wants discharge positive.
            current=-a['current'],
            soc=a['soc'],
            charge=a['remaining_ah'],
            capacity=full_ah if full_ah > 0 else math.nan,
            soh=a['soh'] if 0 < a['soh'] <= 100 else math.nan,
            num_cycles=a['cycles'],
            temperatures=self._last_temps,
            mos_temperature=a['t_mos'],
            battery_charging=bool(s['current'] & CS_CHARGING) if s else None,
            switches=(dict(charge=bool(s['fet'] & FET_CHARGE_ON), discharge=bool(s['fet'] & FET_DISCHARGE_ON))
                      if s else None),
            problem_code=problem_code(s) if s else None,
            balancing_cells=a['balancing'],
        )

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps


async def _locked(lock, coro_fn):
    async with lock():
        return await coro_fn()
