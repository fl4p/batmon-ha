"""JK-PB inverter BMS (JK-PB1A16S, PB2A16S, ...) over RS485 (wired).

The PB series speaks Modbus-ish RTU on its RS485-1 port: the host writes a
one-register FC16 request and the BMS answers with the same 300-byte
``55 AA EB 90 <type> ...`` frame that the JK BLE protocol uses (JK02_32S layout,
8-bit sum checksum in the last byte), followed by the 8-byte FC16 ACK.

    request   <addr> 10 16 20 00 01 02 00 00 <crc16 lo hi>   -> type 0x02 status frame
              <addr> 10 16 1E 00 01 02 00 00 <crc16 lo hi>   -> type 0x01 settings frame
              <addr> 10 16 1C 00 01 02 00 00 <crc16 lo hi>   -> type 0x03 device info

Because the payload is the BLE 32s frame, this driver is ``JKBt_32s`` with the
transport swapped: framing via ``jikong.feed_frames`` and decoding via
``JKBt._decode_sample`` are shared with the BLE path, so the JK-PB RS485 path
gets every BLE fix for free (and vice versa).

Sources:
- mr-manuel/venus-os_dbus-serialbattery ``bms/jkbms_pb.py`` (request bytes,
  frame type per command, offsets 150/158/173/174/182/190/198/199 which are
  the BLE 32s offsets +0, and the 120 ms inter-command gap)
- syssi/esphome-jk-bms ``jk_rs485_bms`` component (same layout, 115200 8N1)

Configure with:
    address: serial
    adapter: /dev/ttyUSB0
    type:    jk_pb_uart          # or jk_pb_uart:2 for RS485 address 2
    alias:   any human-readable name

Several PB units can share one bus, each with its own address set in the JK
app ("RS485 address", 0-15). Untested on hardware, see the README.
"""
import asyncio
import time
from typing import Optional

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.modbus_rtu import append_crc
from bmslib.bt import BtBms
from bmslib.models.jikong import JKBt, feed_frames, FRAME_SIZE
from bmslib.util import to_hex_str

REG_STATUS = 0x1620
REG_SETTINGS = 0x161E
REG_ABOUT = 0x161C
FRAME_TYPE_FOR_REG = {REG_STATUS: 0x02, REG_SETTINGS: 0x01, REG_ABOUT: 0x03}


def build_request(addr: int, reg: int) -> bytes:
    """FC16 write of one zero register: the PB's "send me frame X" trigger."""
    if not 0 <= addr <= 0xFF:
        raise ValueError("JK-PB RS485 address must be 0..255, got %r" % (addr,))
    return append_crc(bytes([addr, 0x10, reg >> 8, reg & 0xFF, 0x00, 0x01, 0x02, 0x00, 0x00]))


class JkPbUart(JKBt):
    BAUDRATE = 115200
    SERIAL_KWARGS = dict(eol=None, timeout=1)
    TIMEOUT = 6
    # Gap before each request. dbus-serialbattery measured 50 ms as marginal on
    # CH341 adapters and 120 ms as error-free on a 4-battery bus.
    REQUEST_SETTLE = 0.12

    def __init__(self, address, **kwargs):
        spec = kwargs.pop('type_spec', None)
        self.bus_addr = int(spec, 0) if spec else 1
        build_request(self.bus_addr, REG_STATUS)  # validate now
        super().__init__(address, **kwargs)
        self.is_new_11fw_32s = True  # PB firmware is 32s layout by definition
        self._pending: Optional[int] = None  # frame type we are waiting for
        self._settle_t = 0.0

    def _notification_handler(self, _sender, data):
        data = bytes(data)
        frames, dropped, corrupt = feed_frames(self._buffer, data)
        if len(self._buffer) >= FRAME_SIZE:
            self._buffer.clear()
        for frame in corrupt:
            self.logger.warning("%s crc check failed, discarding frame: %s...", self.name, to_hex_str(frame[:16]))
        for frame in frames:
            # The 300-byte frame carries no bus address. On a shared bus every
            # unit sees every reply, so only take the one we asked for while
            # our request is outstanding; the bus lock makes that unambiguous.
            if self._pending is None or frame[4] != self._pending:
                self.logger.debug("%s ignoring unsolicited frame 0x%02x", self.name, frame[4])
                continue
            self._decode_msg(bytearray(frame))

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        from bmslib.wired import SerialCharStub
        char = SerialCharStub("jk-pb-uart-%d" % self.bus_addr, "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await BtBms.disconnect(self)  # skip JKBt.disconnect, which stops a BLE char handle

    async def _request(self, reg: int) -> bytearray:
        resp_type = FRAME_TYPE_FOR_REG[reg]
        req = build_request(self.bus_addr, reg)

        async def exchange():
            gap = self.REQUEST_SETTLE - (time.monotonic() - self._settle_t)
            if gap > 0:
                await asyncio.sleep(gap)
            self._pending = resp_type
            try:
                with await self._fetch_futures.acquire_timeout(resp_type, timeout=self.TIMEOUT / 2):
                    await self.client.write_gatt_char(self.UUID_TX, req)
                    return await self._fetch_futures.wait_for(resp_type, self.TIMEOUT)
            finally:
                self._pending = None
                self._settle_t = time.monotonic()

        lock = getattr(self.client, 'bus_lock', None)
        if lock is None:
            return await exchange()
        async with lock():
            return await exchange()

    # --- JKBt API on top of the RS485 exchange -------------------------------

    async def _q(self, cmd, resp):
        # JKBt.fetch() calls _q(cmd=0x96, resp=0x01) for the settings frame.
        if isinstance(resp, tuple):
            for r in resp:
                await self._q(cmd, r)
            return
        reg = {0x01: REG_SETTINGS, 0x02: REG_STATUS, 0x03: REG_ABOUT}[resp]
        await self._request(reg)

    async def _write(self, address, value):
        raise NotImplementedError("writing JK-PB registers over RS485 is not implemented")

    async def fetch_device_info(self) -> DeviceInfo:
        # same 0x03 frame as BLE: model at 6, hw 22, sw 30, name 46, serial 86
        # (dbus-serialbattery jkbms_pb.py reads 6/22/30/46 from the same frame)
        if 0x03 not in self._resp_table:
            await self._request(REG_ABOUT)
        return await super().fetch_device_info()

    async def fetch(self, wait=True) -> BmsSample:
        await self._request(REG_STATUS)
        if 0x01 not in self._resp_table:
            await self._request(REG_SETTINGS)
        if self.num_cells is None:
            buf_set, _ = self._resp_table[0x01]
            self.num_cells = int.from_bytes(buf_set[114:118], 'little') or None
        return await super().fetch(wait=False)

    async def fetch_voltages(self):
        if self.num_cells is None:
            await self.fetch()
        if self.num_cells is None:
            # settings frame did not say; count populated slots instead
            buf, _ = self._resp_table[0x02]
            return [mv for mv in (int.from_bytes(buf[6 + i * 2:8 + i * 2], 'little') for i in range(32)) if mv]
        return await super().fetch_voltages()

    async def set_switch(self, switch: str, state: bool):
        raise NotImplementedError("JK-PB switches over RS485 are not implemented")

    def supports_set_soc(self) -> bool:
        return False
