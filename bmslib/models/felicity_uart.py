"""Felicity Solar 48 V LiFePO4 packs (FLB48314TG1, FLB48100, LPBF48 / LPBA48, ...)
over RS485 (wired), Voltronic "LIB" BMS protocol: Modbus RTU function 0x03 with a
NON-STANDARD response header.

The BLE path (`type: felicity`, via aiobmsble) needs bonding and loses the link to
scan contention and per-request timeouts (#415). The same packs expose the protocol
below on pins 5 (B) and 6 (A) of their RJ45 communication port, which avoids all
three -- no adapter, no scan, no bonding. Whether it is more reliable in practice is
unmeasured: see "Untested on hardware" at the end.

Framing (the one thing that is not ordinary Modbus):

    request   <slave> 03 <reg hi> <reg lo> <count hi> <count lo> <crc lo> <crc hi>
    response  <slave> 03 <COUNT hi> <COUNT lo> <data ...>        <crc lo> <crc hi>
    exception <slave> 83 <code> <crc lo> <crc hi>

The two bytes after the function code are a 16-bit REGISTER count, not the 8-bit
byte count of standard Modbus, so the frame is `4 + 2*count + 2` bytes long. Every
off-the-shelf Modbus client -- and `bmslib.modbus_rtu.extract_read_frame`, which the
other wired Modbus drivers use -- rejects such a frame. Hence the private
`extract_frame` below; the shared helper is left exactly as the other drivers found
it. CRC-16/Modbus, 9600 8N1, reply 60-80 ms after the request.

Register map (holding registers, big-endian; `verified` = seen on the wire in the
capture referenced under Sources, `document` = Voltronic document only):

    1 (0x01)        protocol type                               document (reads 0)
    2 (0x02)        protocol version                            document (reads 0)
    3-4 (0x03)      BMS firmware version, u32 (hi.lo -> "1.1")  document
    5-6 (0x05)      BMS hardware version, u32                   document
    16 (0x10)       number of cells                             verified (16)
    17-36           cell voltage 1..20, mV on Felicity          verified
                    (the Voltronic document says 0.1 V -- see below)
    37 (0x25)       number of temperature sensors               verified (4)
    38-47           NTC 1..10, 0.1 K -> degC = raw/10 - 273.15  verified
    48 (0x30)       charge current, 0.1 A (0 while discharging) verified
    49 (0x31)       discharge current, 0.1 A (0 while charging) verified
    50 (0x32)       pack voltage, mV on Felicity (doc: 0.1 V)   verified
    51 (0x33)       SOC, %                                      verified
    52-53 (0x34)    full-charge capacity, u32, mAh              verified (350000)
    55-60 (0x37)    alarm and protection bits  NOT READ, see the note below
    112 (0x70)      charge voltage limit, 0.1 V                 verified (576)
    113 (0x71)      discharge voltage limit, 0.1 V              verified (480)
    114 (0x72)      charge current limit, 0.1 A                 verified
    115 (0x73)      discharge current limit, 0.1 A              verified (1600)
    116 (0x74)      charge/discharge status bits                verified (0xC0)
                    bit7 charge enable, bit6 discharge enable, bit5/4 charge
                    immediately, bit3 full charge request, bit2 small-current charge

Scaling deviations from the Voltronic document, and how they are handled: Felicity
firmware reports cell and pack voltage in mV where the document says 0.1 V. Both are
accepted and told apart by magnitude, the way the source project's `felicityd` does
it: a first cell above 1000 counts means mV, a pack voltage of 10000 or more means
mV. Capacity (mAh), currents (0.1 A), temperatures (0.1 K) and the limits (0.1 V /
0.1 A) agree with the document and are used as documented.

The version block (1..6) is read only for the device-info entry; no frame of it
appears in the capture, so its layout rests on the two documents alone. Registers
54..111 and 117..119 are not read at all.

Sources: protocol decoded, documented and verified on three FLB48314TG1 packs by
Krzysztof Hajdamowicz,
https://github.com/KrzysztofHajdamowicz/felicity-battery-modbus (MIT) -- its
`docs/protocol.md`, the reference decoder `tools/felicity_decoder.py` and the live
bus capture `captures/2026-10-10-rs485-bus.log`, which the unit tests replay. The
underlying vendor document is Voltronic Power, "Inverter and BMS 485 communication
Protocol", 2020-03-25.

That work is MIT licensed:

    Copyright (c) 2026 Krzysztof Hajdamowicz

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

The protocol knowledge is ported, not their code; the register map and the frame
layout above are a re-expression of their documentation.

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    felicity_uart          # or felicity_uart:2 for DIP address 2
    alias:   any human-readable name

Several packs share one bus, each with its own DIP address (1, 2, 3, ...); add one
device per pack on the same adapter. Default address 1. 9600 8N1.
Untested on hardware.
"""
import time
from typing import List, Optional, Tuple

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms
from bmslib.modbus_rtu import build_read, check_crc, ModbusError, u16_be, u32_be, FC_READ_HOLDING

REG_VERSION = 0x01      # 1, read 6: protocol type/version, fw u32, hw u32
REG_CELLS = 0x10        # 16, read 21: cell count + 20 cell voltages
REG_STATUS = 0x25       # 37, read 17: temp count, 10 NTC, currents, voltage, soc, capacity
REG_LIMITS = 0x70       # 112, read 5: voltage/current limits + status bits

MAX_CELLS = 20
MAX_TEMPS = 10

# The limits and the status bits change slowly (the source project polls them once
# a minute to once every five); on a shared bus every extra read costs every pack a
# slot, so they get one read a minute, failures included.
LIMITS_PERIOD = 60


def extract_frame(buf: bytearray, slave: int,
                  count: Optional[int] = None) -> Optional[Tuple[int, int, bytes]]:
    """Pop the first complete Felicity FC3 response (or exception) off ``buf``.

    Returns ``(slave, fc, data)`` or ``None`` while the frame is still incomplete,
    and raises :class:`ModbusError` for an exception reply. ``count`` is the number
    of registers the outstanding request asked for; passing it rejects anything of
    another length before its CRC is even looked at.

    Both header shapes are accepted, told apart without ambiguity: a standard
    Modbus byte count is never zero, while the Felicity register count's high byte
    always is (no read here is 256 registers or longer). A frame that is one byte
    short -- the source project saw ~10 % of those on a USB-RS485 adapter -- fails
    the CRC and is dropped; the next poll asks again.

    Two ways a frame can be preceded by rubbish, both of which have to leave the
    real reply reachable:

    * Many USB-RS485 adapters are half duplex and echo what we transmit. An echoed
      request begins `<slave> 03 <start hi> <start lo>`, which reads as a response
      header promising `start_lo` registers -- a frame far longer than the buffer
      will ever hold. Waiting for it would swallow the real reply sitting right
      behind it and time out every single poll. So before parking on an incomplete
      candidate we look for a complete, CRC-valid frame further along and resync to
      it; only a buffer with no such frame in it is worth waiting on.
    * Another pack's reply is consumed whole rather than scanned into, so a byte
      sequence inside its payload that happens to look like a frame of ours cannot
      be mistaken for our reply.
    """
    while True:
        if len(buf) < 5:
            return None
        if buf[0] != slave:
            _skip_foreign(buf)
            continue
        fc = buf[1]
        if fc & 0x80:
            frame = bytes(buf[:5])
            if check_crc(frame):
                del buf[:5]
                raise ModbusError("felicity: modbus exception 0x%02x from slave %d (fc 0x%02x)"
                                  % (frame[2], frame[0], fc & 0x7F))
            del buf[0]
            continue
        if fc != FC_READ_HOLDING:
            del buf[0]
            continue
        shape = _frame_shape(buf, count)
        if shape is None:
            del buf[0]
            continue
        head, n_data = shape
        total = head + n_data + 2
        if len(buf) < total:
            skip = _find_frame(buf, slave, count)
            if skip is None:
                return None             # genuinely incomplete: wait for more bytes
            del buf[:skip]              # an echo, or noise, in front of a real frame
            continue
        frame = bytes(buf[:total])
        if not check_crc(frame):
            del buf[0]
            continue
        del buf[:total]
        return frame[0], fc, frame[head:head + n_data]


def _frame_shape(buf, count: Optional[int]) -> Optional[Tuple[int, int]]:
    """``(header length, data length)`` of the FC3 response ``buf`` starts with, or
    None if its header cannot belong to a reply to a ``count``-register read."""
    if buf[2] == 0:                             # Felicity: 16-bit register count
        n_regs = buf[3]
        if not 1 <= n_regs <= 125 or (count is not None and n_regs != count):
            return None
        return 4, n_regs * 2
    if count is not None and buf[2] != count * 2:   # standard Modbus byte count
        return None
    return 3, buf[2]


def _find_frame(buf, slave: int, count: Optional[int]) -> Optional[int]:
    """Offset of the first complete, CRC-valid frame for ``slave`` after the head of
    ``buf``, or None if there is none to resync to."""
    for i in range(1, len(buf) - 4):
        if buf[i] != slave:
            continue
        fc = buf[i + 1]
        if fc & 0x80:
            if check_crc(bytes(buf[i:i + 5])):
                return i
            continue
        if fc != FC_READ_HOLDING:
            continue
        shape = _frame_shape(buf[i:i + 4], count)   # i <= len(buf)-5, so 4 bytes exist
        if shape is None:
            continue
        total = shape[0] + shape[1] + 2
        if len(buf) - i >= total and check_crc(bytes(buf[i:i + total])):
            return i
    return None


def _skip_foreign(buf) -> None:
    """Drop the frame another unit on the bus put at the head of ``buf`` -- whole if
    it parses and its CRC checks out, one byte otherwise."""
    fc = buf[1]
    if fc & 0x80:
        if check_crc(bytes(buf[:5])):
            del buf[:5]
            return
    elif fc == FC_READ_HOLDING and len(buf) >= 4:
        shape = _frame_shape(buf, None)
        if shape is not None:
            total = shape[0] + shape[1] + 2
            if len(buf) >= total and check_crc(bytes(buf[:total])):
                del buf[:total]
                return
    del buf[0]


def _temperature(raw: int) -> float:
    """0.1 K -> degC. A raw value of 1000 or less is read as whole kelvin, which is
    what the reference decoder does for firmware that reports 1 K steps."""
    return (raw / 10 if raw > 1000 else float(raw)) - 273.15


def decode_cell_block(data: bytes) -> dict:
    """Registers 16..36: cell count and 20 cell voltages."""
    if len(data) < 42:
        raise ValueError(f"felicity cell block too short: {len(data)} bytes")
    n_cells = u16_be(data, 0)
    if not 1 <= n_cells <= MAX_CELLS:
        raise ValueError(f"felicity: implausible cell count {n_cells}")
    raw = [u16_be(data, 2 + 2 * i) for i in range(n_cells)]
    # The vendor document gives 0 both to a cell the pack does not have and to a
    # parameter the firmware does not implement, so a zero inside the declared
    # cells is missing data, not a 0 V cell. Publishing it would invent a cell at
    # 0 V and a pack-wide voltage spread of 3.3 V; reject the block instead, and
    # keep the cell indexes of the ones that did arrive intact.
    missing = [i + 1 for i, v in enumerate(raw) if v == 0]
    if missing:
        raise ValueError("felicity: cell%s %s of %d read 0"
                         % ('s' if len(missing) > 1 else '',
                            ','.join(map(str, missing)), n_cells))
    # mV (Felicity) or 0.1 V (Voltronic document), told apart by magnitude
    mult = 1 if max(raw) > 1000 else 100
    return dict(cell_mv=[v * mult for v in raw])


def decode_status_block(data: bytes) -> dict:
    """Registers 37..53: temperatures, currents, pack voltage, SOC, capacity."""
    if len(data) < 34:
        raise ValueError(f"felicity status block too short: {len(data)} bytes")
    n_temps = u16_be(data, 0)
    if not 0 <= n_temps <= MAX_TEMPS:
        raise ValueError(f"felicity: implausible temperature sensor count {n_temps}")
    temps = [_temperature(raw) for raw in
             (u16_be(data, 2 + 2 * i) for i in range(n_temps)) if raw]
    i_charge = u16_be(data, 22) / 10
    i_discharge = abs(u16_be(data, 24, signed=True)) / 10
    v_raw = u16_be(data, 26)
    if v_raw == 0:
        raise ValueError("felicity: pack voltage reads 0")
    return dict(
        temps=temps,
        # BmsSample sign: positive = out of the battery
        current=i_discharge - i_charge,
        voltage=v_raw / 1000 if v_raw >= 10000 else v_raw / 10,
        soc=u16_be(data, 28),
        capacity=u32_be(data, 30) / 1000,   # mAh -> Ah
    )


def decode_limits_block(data: bytes) -> dict:
    """Registers 112..116: charge/discharge limits and the status bits."""
    if len(data) < 10:
        raise ValueError(f"felicity limits block too short: {len(data)} bytes")
    status = u16_be(data, 8)
    return dict(
        charge_voltage_limit=u16_be(data, 0) / 10,
        discharge_voltage_limit=u16_be(data, 2) / 10,
        charge_current_limit=u16_be(data, 4) / 10,
        discharge_current_limit=u16_be(data, 6) / 10,
        status=status,
        charge_enable=bool(status & 0x80),
        discharge_enable=bool(status & 0x40),
    )


# Registers 55..60 (alarms and protections) are deliberately NOT read or
# published. Nothing in either document can be taken at face value here: the
# Voltronic bit assignments have never been seen to fire on Felicity firmware,
# at least one documented bit (0x39 bit0 PRES, the "in system" signal) is not a
# fault at all, and the vendor document says an unimplemented parameter reads
# 0x0000 -- so an all-zero block cannot be read as "healthy" either. Both
# directions would be a fabricated verdict: a presence or reserved bit would
# raise a permanent fault, and silence would publish a clean bill of health the
# pack never gave. `problem` therefore stays None (unknown) until someone
# confirms the semantics against a pack that is actually in alarm.


def decode_version_block(data: bytes) -> dict:
    """Registers 1..6: protocol type/version and firmware/hardware version."""
    if len(data) < 12:
        raise ValueError(f"felicity version block too short: {len(data)} bytes")

    def ver(off):
        return '%d.%d' % (u16_be(data, off), u16_be(data, off + 2))

    return dict(protocol_type=u16_be(data, 0), protocol_version=u16_be(data, 2),
                sw_version=ver(4), hw_version=ver(8))


class FelicityUart(BtBms):
    BAUDRATE = 9600
    SERIAL_KWARGS = dict(eol=None, timeout=1)
    TIMEOUT = 6
    _KEY = 'felicity'

    def __init__(self, address, **kwargs):
        spec = kwargs.pop('type_spec', None)
        self.slave = int(spec, 0) if spec else 1
        if not 1 <= self.slave <= 247:
            raise ValueError("felicity address must be 1..247 (the DIP switch number), got %r" % (spec,))
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_cells: List[int] = []
        self._last_temps: List[float] = []
        self._limits: Optional[dict] = None
        self._limits_t = 0.0
        self._expect: Optional[int] = None

    def _resolve(self, value) -> bool:
        """Hand one reply to the pending request, if it is still pending.

        FuturesPool.set_result *removes* an already-resolved future, so resolving
        twice (two frames in one callback, a duplicate reply, a late reply racing
        the next request) would leave the next wait_for with no future at all and
        raise KeyError out of an unrelated read.
        """
        if not self._fetch_futures.is_waiting(self._KEY):
            self.logger.debug("%s felicity: unsolicited frame dropped", self.name)
            return False
        self._fetch_futures.set_result(self._KEY, value)
        return True

    def _notification_handler(self, sender, data):
        self._buffer += bytes(data)
        while True:
            try:
                res = extract_frame(self._buffer, slave=self.slave, count=self._expect)
            except ModbusError as exc:
                self.logger.warning("%s %s", self.name, exc)
                self._resolve(exc)
                continue
            if res is None:
                return
            _slave, _fc, payload = res
            self._resolve(payload)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("felicity-uart-%d" % self.slave, "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await super().disconnect()

    async def _read(self, reg: int, count: int) -> bytes:
        req = build_read(self.slave, reg, count, FC_READ_HOLDING)

        async def exchange():
            # Start every exchange clean: a stale partial frame must not be able
            # to shift where the reply is looked for. (A half-duplex adapter's
            # echo of this very request arrives *after* the clear; the extractor
            # resyncs past it.)
            self._buffer.clear()
            self._expect = count
            try:
                with self._fetch_futures.acquire(self._KEY):
                    await self.client.write_gatt_char(self.UUID_TX, data=req)
                    res = await self._fetch_futures.wait_for(self._KEY, self.TIMEOUT)
            finally:
                self._expect = None
            if isinstance(res, Exception):
                raise res
            if len(res) != count * 2:
                raise ValueError(f"felicity reg {reg}: expected {count * 2} bytes, got {len(res)}")
            return res

        # RS485 is half duplex and several packs share the bus: hold the port's
        # lock for the whole request/response round trip.
        lock = getattr(self.client, 'bus_lock', None)
        if lock is None:
            return await exchange()
        async with lock():
            return await exchange()

    async def _fetch_limits(self) -> Optional[dict]:
        """Limits and status bits, one read per LIMITS_PERIOD at most.

        The clock is set whether the read worked or not: a pack that fails this
        block must not be asked again on every single sample, which on a shared
        bus would cost every other pack a slot per poll.
        """
        now = time.time()
        if now - self._limits_t < LIMITS_PERIOD:
            return self._limits
        self._limits_t = now
        try:
            self._limits = decode_limits_block(await self._read(REG_LIMITS, 5))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("%s felicity limits read failed: %s", self.name, exc)
            self._limits = None
        return self._limits

    async def fetch(self) -> BmsSample:
        st = decode_status_block(await self._read(REG_STATUS, 17))
        cells = decode_cell_block(await self._read(REG_CELLS, 21))
        limits = await self._fetch_limits()

        self._last_cells = cells['cell_mv']
        self._last_temps = st['temps']
        switches = None
        if limits:
            switches = dict(charge=limits['charge_enable'], discharge=limits['discharge_enable'])
        return BmsSample(
            voltage=st['voltage'],
            current=st['current'],
            capacity=st['capacity'],
            soc=st['soc'],
            temperatures=st['temps'] or None,
            switches=switches,
            # no alarm registers are read; see the note above decode_version_block
            problem=None,
        )

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps

    async def fetch_device_info(self) -> DeviceInfo:
        v = decode_version_block(await self._read(REG_VERSION, 6))
        return DeviceInfo(mnf='Felicity Solar', model='Felicity 48V LiFePO4',
                          hw_version=v['hw_version'], sw_version=v['sw_version'],
                          name=self.name, sn=None)

    async def set_switch(self, switch: str, state: bool):
        raise NotImplementedError("Felicity packs expose no writable registers over RS485")

    def __str__(self):
        # several packs share one `serial` address, so the DIP address belongs
        # in every log line next to the name
        return 'FelicityUart(%s,%s,addr=%d)' % (self.address, self.name, self.slave)
