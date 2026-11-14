"""ANT BMS over its wired UART port (TTL, or RS485 via an adapter).

ANT boards speak one of two protocols on the wire:

``ant_uart`` -- 2021+ boards (16ZMB, 24AHA, 24BH, ...), "7E A1" protocol
    The same frames as over BLE (the BLE module is a UART bridge), so the
    decoder is ``AntBt.fetch`` from ``bmslib.models.ant``:
        request  7E A1 <func> <addr lo hi> <len> <crc16 lo hi> AA 55
        reply    7E A1 <func|0x10> <addr lo hi> <len> <payload> <crc16 lo hi> AA 55 [extra section]
    CRC16/Modbus over bytes 1..5+len. Status: func 0x01 -> 0x11 (152 bytes for
    up to 32 cells), device info: func 0x02 @0x026C -> 0x12. Writes (MOSFET
    switches) are func 0x51, ACKed with 0x61, each preceded by the auth frame
    (func 0x23 @0x016A, default password, ACKed with 0x43).

``ant_old_uart`` -- pre-2021 boards, "AA 55 AA FF" protocol
    request  5A 5A 00 00 01 01        (5A 5A = read, address 0x00, data 0x0001, sum)
    reply    140 bytes, AA 55 AA FF + big-endian fields + 16-bit sum of bytes
             4..137 (big-endian) at 138..139; layout in ``decode_old_status``.
    Current at 70..73 is int32 * 0.1 A, positive = discharging (batmon's own
    sign): in the issue #28 capture the remaining charge rises while it reads
    -7.4 A. Some 2021 boards answer this request too, with power as a 9999 W
    placeholder (not used here); current is real (issue #37). For a 2021 board
    this is the wire path with captured evidence: ``7E A1`` has only been seen
    from the PC tool (issue #20, no BMS attached) and over a Bluetooth serial
    port (issue #18), never as a reply on the wire.

Link: 19200 8N1. That is what syssi/esphome-ant-bms runs and what every captured
exchange used, on 2020 and 2021 boards. The vendor protocol PDF and the vendor
PC tool default to 9600, so the baud rate can be overridden with the type
suffix, e.g. ``type: ant_uart:9600``. The 4-pin JST 1.25 mm port is 3.3 V TTL
(VCC, GND, RX, TX); the BMS only answers with VCC connected.

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    ant_uart            # or ant_old_uart, optionally :<baud>
    alias:   any human-readable name

Sources:
- syssi/esphome-ant-bms (Apache-2.0): ``ant_bms`` (7E A1 over UART since 3.0.0),
  ``ant_bms_old`` (AA 55 AA FF), test frames, and the vendor PDF in ``docs/``.
  Captured UART traffic with decoded values in issues #28, #37 (old protocol)
  and the vendor PC tool's 7E A1 serial traffic in issues #18 and #20.
- yosle/ant-bms-ble PROTOCOL.md (MIT): 7E A1 layout, old-protocol appendix
  (offset 91 = MOS temperature, 93 = balancer, 95.. = NTCs).
- klotztech/VBMS wiki "Serial protocol": old-protocol checksum and fields.

Untested on hardware here: both decoders are checked against captured frames
only (bmslib/test/test_ant_uart.py).
"""
import asyncio
import math
from typing import List, Optional

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.ble.bt import BtBms
from bmslib.models.ant import AntBt, AntCommandFuncs, _ant_auth_frame, _ant_command, calc_crc16
from bmslib.util import to_hex_str

BAUDRATE = 19200
_HEADER = b'\x7e\xa1'


def _baud_from_spec(spec: Optional[str]) -> int:
    if not spec:
        return BAUDRATE
    baud = int(spec, 0)
    if baud not in (2400, 4800, 9600, 19200, 38400, 57600, 115200):
        raise ValueError("ANT baud rate %r is not a standard rate" % (spec,))
    return baud


async def _locked(client, coro_fn):
    # Half duplex: hold the bus for the whole exchange when the port is shared.
    lock = getattr(client, 'bus_lock', None)
    if lock is None:
        return await coro_fn()
    async with lock():
        return await coro_fn()


def feed_frames(buf: bytearray):
    """Pull complete 7E A1 frames out of a serial byte stream.

    Returns ``(frames, corrupt)``. A frame ends at its CRC; the AA 55 tail and
    the "extra section" some replies carry after it are skipped while looking
    for the next header. A CRC failure drops one byte and resyncs.
    """
    frames, corrupt = [], []
    while True:
        i = buf.find(_HEADER)
        if i < 0:
            # keep a trailing 0x7E, it may be the first header byte
            del buf[:len(buf) - 1 if buf.endswith(b'\x7e') else len(buf)]
            return frames, corrupt
        del buf[:i]
        if len(buf) < 6:
            return frames, corrupt
        end = 6 + buf[5] + 2  # header + payload + crc
        if len(buf) < end:
            return frames, corrupt
        frame = bytes(buf[:end])
        if bytes(calc_crc16(frame[1:end - 2])) == frame[end - 2:]:
            frames.append(frame + b'\xaa\x55')
            del buf[:end]
        else:
            corrupt.append(frame)
            del buf[:1]


class AntUart(AntBt):
    """2021+ ANT boards, 7E A1 protocol, decoded by ``AntBt``."""
    BAUDRATE = BAUDRATE
    SERIAL_KWARGS = dict(eol=None, timeout=1)
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        self.BAUDRATE = _baud_from_spec(kwargs.pop('type_spec', None))
        super().__init__(address, **kwargs)

    def _notification_handler(self, sender, data: bytes):
        # Serial reads arrive in arbitrary chunks with no relation to frame
        # boundaries, so AntBt's per-notification framing does not apply.
        self._buffer += bytes(data)
        frames, corrupt = feed_frames(self._buffer)
        for frame in corrupt:
            self.logger.debug("%s CRC mismatch, resyncing: %s...", self.name, to_hex_str(frame[:16]))
        for frame in frames:
            self._last_response = bytearray(frame)
            func = frame[2]
            if self._fetch_futures.is_waiting(func):
                self._fetch_futures.set_result(func, bytearray(frame))

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.uart import SerialCharStub
        self.char = SerialCharStub("ant-uart", "notify")
        await self.client.start_notify(self.char, self._notification_handler)

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.char)
        except Exception:
            pass
        await BtBms.disconnect(self)  # AntBt.disconnect stops a GATT char

    async def _q(self, cmd: AntCommandFuncs, addr, val, resp_code):
        async def exchange():
            # junk with a false 7E A1 header would otherwise hold this reply back for the next poll
            self._buffer.clear()
            with await self._fetch_futures.acquire_timeout(resp_code, timeout=self.TIMEOUT / 2):
                await self.client.write_gatt_char(self.char, data=_ant_command(cmd, addr, val))
                return await self._fetch_futures.wait_for(resp_code, self.TIMEOUT)

        return await _locked(self.client, exchange)

    async def set_switch(self, switch: str, state: bool):
        register_onoff = dict(charge=[0x0006, 0x0004], discharge=[0x0003, 0x0001], balance=[0x000D, 0x000E],
                              buzzer=[0x001E, 0x001F])
        addr = register_onoff[switch][0 if state else 1]

        async def write():
            # without the auth frame the BMS reverts a charge-off within seconds (BLE, ant24)
            await self.client.write_gatt_char(self.char, data=_ant_auth_frame())
            await self.client.write_gatt_char(self.char, data=_ant_command(AntCommandFuncs.WriteRegister, addr, 0))

        await _locked(self.client, write)


# --- pre-2021 protocol -------------------------------------------------------

def _ntc_present(ntc):
    """-40 marks an absent probe. 2020 boards fill unused slots with 0 instead (issue #28), so 0 in a
    trailing slot after a non-zero probe is absent too; a lone or leading 0 stays a real 0 degC."""
    out = [math.nan if t == -40 else t for t in ntc]
    for i in range(len(out) - 1, 0, -1):
        if out[i] == 0 and any(t not in (0,) and not math.isnan(t) for t in out[:i]):
            out[i] = math.nan
        elif not math.isnan(out[i]):
            break
    return out


OLD_REQUEST_STATUS = bytes.fromhex('5a5a00000101')
OLD_HEADER = b'\xaa\x55\xaa\xff'
OLD_FRAME_LEN = 140


def old_checksum_ok(frame: bytes) -> bool:
    return sum(frame[4:138]) & 0xFFFF == int.from_bytes(frame[138:140], 'big')


def feed_old_frames(buf: bytearray):
    """Pull 140-byte AA 55 AA FF frames out of a serial byte stream.

    Returns ``(frames, corrupt)``; a checksum failure drops one byte and resyncs.
    Trailing junk (some adapters add FF bytes after the frame) is skipped.
    """
    frames, corrupt = [], []
    while True:
        i = buf.find(OLD_HEADER)
        if i < 0:
            keep = 0
            for k in (3, 2, 1):  # a header split across reads
                if buf.endswith(OLD_HEADER[:k]):
                    keep = k
                    break
            del buf[:len(buf) - keep]
            return frames, corrupt
        del buf[:i]
        if len(buf) < OLD_FRAME_LEN:
            return frames, corrupt
        frame = bytes(buf[:OLD_FRAME_LEN])
        if old_checksum_ok(frame):
            frames.append(frame)
            del buf[:OLD_FRAME_LEN]
        else:
            corrupt.append(frame)
            del buf[:1]


def decode_old_status(f: bytes) -> dict:
    """Decode a 140-byte AA 55 AA FF status frame (all fields big-endian).

    Offsets (vendor PDF, klotztech VBMS wiki, yosle appendix):
      4 u16 pack voltage 0.1 V        6 u16[32] cells mV       70 i32 current 0.1 A
      74 u8 SOC %                     75 u32 capacity uAh      79 u32 remaining uAh
      83 u32 cycle (throughput) mAh   87 u32 uptime s          91 i16 MOS temp
      93 i16 balancer temp            95 i16[4] NTCs (-40: absent)
      103/104/105 u8 charge/discharge/balancer status          111 i32 power W
      115 u8 + 116 u16 max cell,  118 u8 + 119 u16 min cell,   121 u16 avg cell
      123 u8 cell count   132 u32 balancing bitmask   136 u16 system log   138 u16 sum
    """
    if len(f) != OLD_FRAME_LEN or not f.startswith(OLD_HEADER):
        raise ValueError('not an ANT status frame: %s' % to_hex_str(f[:8]))
    u = lambda i, n: int.from_bytes(f[i:i + n], 'big')
    s = lambda i, n: int.from_bytes(f[i:i + n], 'big', signed=True)
    n = f[123]
    if not 1 <= n <= 32:
        raise ValueError('ANT status frame with %d cells' % n)
    ntc = [s(95 + 2 * i, 2) for i in range(4)]
    return dict(
        voltage=u(4, 2) / 10,
        cells=[u(6 + 2 * i, 2) for i in range(n)],
        current=s(70, 4) / 10,
        soc=f[74],
        capacity=u(75, 4) / 1e6,
        charge=u(79, 4) / 1e6,
        cycle_charge=u(83, 4) / 1e3,
        uptime=u(87, 4),
        mos_temperature=s(91, 2),
        balancer_temperature=s(93, 2),
        temperatures=_ntc_present(ntc),
        charge_status=f[103],
        discharge_status=f[104],
        balancer_status=f[105],
        power=s(111, 4),
        balancing_mask=u(132, 4),
    )


class AntOldUart(BtBms):
    """Pre-2021 ANT boards, AA 55 AA FF protocol. Read-only."""
    BAUDRATE = BAUDRATE
    SERIAL_KWARGS = dict(eol=None, timeout=1)
    TIMEOUT = 8
    _KEY = 'ant_old'

    def __init__(self, address, **kwargs):
        self.BAUDRATE = _baud_from_spec(kwargs.pop('type_spec', None))
        kwargs.setdefault('_uses_pin', False)
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._cells: List[int] = []
        self._last_response: Optional[bytes] = None
        self.char = None

    def _notification_handler(self, sender, data):
        self._buffer += bytes(data)
        frames, corrupt = feed_old_frames(self._buffer)
        for frame in corrupt:
            self.logger.debug("%s checksum mismatch, resyncing: %s...", self.name, to_hex_str(frame[:16]))
        for frame in frames:
            self._last_response = frame
            if self._fetch_futures.is_waiting(self._KEY):
                self._fetch_futures.set_result(self._KEY, frame)

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.uart import SerialCharStub
        self.char = SerialCharStub("ant-old-uart", "notify")
        await self.client.start_notify(self.char, self._notification_handler)

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.char)
        except Exception:
            pass
        await super().disconnect()

    async def _q(self) -> bytes:
        async def exchange():
            self._buffer.clear()
            with await self._fetch_futures.acquire_timeout(self._KEY, timeout=self.TIMEOUT / 2):
                await self.client.write_gatt_char(self.char, data=OLD_REQUEST_STATUS)
                return await self._fetch_futures.wait_for(self._KEY, self.TIMEOUT)

        return await _locked(self.client, exchange)

    async def fetch(self) -> BmsSample:
        d = decode_old_status(await self._q())
        self._cells = d['cells']
        return BmsSample(
            voltage=d['voltage'],
            current=d['current'],
            soc=d['soc'],
            charge=d['charge'],
            # 0 when never configured; would otherwise turn into SoC 0 %
            capacity=d['capacity'] or math.nan,
            total_charge_throughput=d['cycle_charge'],
            temperatures=d['temperatures'],
            mos_temperature=d['mos_temperature'],
            uptime=d['uptime'],
            # power (111) is left out: 2021 boards answering this protocol put 9999 there
        )

    async def fetch_voltages(self) -> List[int]:
        return self._cells

    async def fetch_device_info(self) -> DeviceInfo:
        return DeviceInfo(mnf='ANT', model='ANT BMS (pre-2021 protocol)', hw_version=None, sw_version=None,
                          name=None, sn=None)

    async def set_switch(self, switch: str, state: bool):
        raise NotImplementedError("ant_old_uart is read-only")

    def debug_data(self):
        return self._last_response
