"""JK-PB inverter BMS over RS485, listen-only: for a bus that already has a master.

In a JK-PB parallel group the master BMS (RS485 address 0) polls its slaves on RS485-1 all the
time and also puts its own status and settings frames on the bus. ``jk_pb_uart`` is a second,
active master there: its requests collide with the master's, address 0 never answers it, and the
reply frames carry no address, so the packs' values get mixed up.

This type never writes. It follows the master's polling loop and picks out the frames of its
address (see ``bmslib/uart/jk_pb_bus.py``); every pack of the group is one entry with the same
``adapter:``, including the master with ``jk_pb_uart_passive:0``::

    - address: serial
      adapter: /dev/serial/by-id/usb-...
      type: jk_pb_uart_passive:0     # the master
      alias: JK_0
    - address: serial
      adapter: /dev/serial/by-id/usb-...
      type: jk_pb_uart_passive:1     # slave, address as set on the DIP switches
      alias: JK_1

Device info (frame 0x03) is only sent on request, so there is no model, firmware or serial number,
and the float-charge switch is not offered. Samples arrive once per polling cycle (about 6 s).
"""
import asyncio
from typing import Optional

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.models.jikong import JKBt
from bmslib.models.jk_pb_uart import JkPbUart
from bmslib.uart import SerialCharStub
from bmslib.uart.jk_pb_bus import PbBusParser


class JkPbUartPassive(JkPbUart):
    # Longer than two polling cycles of the master (about 6 s each).
    PASSIVE_TIMEOUT = 15

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._parser = PbBusParser()
        self._consumed_t = 0.0  # arrival time of the status frame the last sample was made from
        self._seen_types = set()
        self._has_float_charger = False  # frame 0x03 never comes by itself, and the switch would need a write

    def _notification_handler(self, _sender, data):
        for fr in self._parser.feed(bytes(data)):
            if fr.owner != self.bus_addr:
                continue
            if fr.ftype not in self._seen_types:
                self._seen_types.add(fr.ftype)
                self.logger.info('%s first frame 0x%02x from RS485 address %d (%s)', self.name, fr.ftype,
                                 fr.owner, 'answer to a request' if fr.solicited else "the master's own")
            self.logger.debug('%s frame 0x%02x solicited=%s', self.name, fr.ftype, fr.solicited)
            self._decode_msg(bytearray(fr.frame))

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        self._parser.reset()
        char = SerialCharStub("jk-pb-uart-passive-%d" % self.bus_addr, "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char

    async def _request(self, reg: int):
        raise NotImplementedError("jk_pb_uart_passive never writes to the bus")

    async def _wait_frame(self, ftype: int):
        entry = self._resp_table.get(ftype)
        if entry and (ftype != 0x02 or entry[1] > self._consumed_t):
            return  # settings are kept; a status frame only once
        try:
            with await self._fetch_futures.acquire_timeout(ftype, timeout=self.PASSIVE_TIMEOUT / 2):
                await self._fetch_futures.wait_for(ftype, self.PASSIVE_TIMEOUT)
        except asyncio.TimeoutError:
            self.logger.warning('%s no frame 0x%02x from RS485 address %d in %d s: %s', self.name, ftype,
                                self.bus_addr, self.PASSIVE_TIMEOUT, self._parser.summary())
            raise

    async def fetch_device_info(self) -> DeviceInfo:
        return DeviceInfo(mnf="JK", model="JK-PB (RS485, listen-only)", hw_version=None, sw_version=None,
                          name=self.name)

    async def fetch(self, wait=True) -> BmsSample:
        if wait:
            await self._wait_frame(0x02)
        if 0x01 not in self._resp_table:
            await self._wait_frame(0x01)
        if self.num_cells is None:
            buf_set, _ = self._resp_table[0x01]
            self.num_cells = int.from_bytes(buf_set[114:118], 'little') or None
        sample = await JKBt.fetch(self, wait=False)  # not JkPbUart.fetch, which sends requests
        self._consumed_t = self._resp_table[0x02][1]
        return sample

    def debug_data(self):
        return dict(super().debug_data(), parser=self._parser.summary())
