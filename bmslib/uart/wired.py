"""Plumbing shared by the wired (RS232/RS485/UART) BMS models."""
from typing import List, Optional, Tuple

from bmslib.ble.bt import BtBms
from bmslib.uart import SerialCharStub


def parse_addr_baud(spec: Optional[str], default_addr: int) -> Tuple[int, Optional[int]]:
    """``type: <model>:<addr>[:<baud>]`` -> (addr, baud or None)."""
    parts = [p for p in (spec or '').split(':') if p]
    addr = int(parts[0], 0) if parts else default_addr
    return addr, (int(parts[1]) if len(parts) > 1 else None)


async def bus_locked(client, coro_fn):
    """Run one request/response exchange holding the port's bus lock.

    RS485 is half duplex and several packs may share the bus, so the lock covers
    the whole round trip. Clients without a bus lock run unlocked."""
    lock = getattr(client, 'bus_lock', None)
    if lock is None:
        return await coro_fn()
    async with lock():
        return await coro_fn()


class WiredBms(BtBms):
    """Base for wired models that decode into ``_last_cells``/``_last_temps``.

    Subclasses set ``self._buffer`` and implement ``_char_name()``, the notify
    key that identifies this unit on a shared port."""

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_cells: List[int] = []
        self._last_temps: List[float] = []

    def _char_name(self) -> str:
        raise NotImplementedError

    async def connect(self, timeout=10, **kwargs):
        await self.client.connect(timeout=timeout)
        self._buffer.clear()
        char = SerialCharStub(self._char_name(), "notify")
        await self.client.start_notify(char, self._notification_handler)
        self.UUID_RX = char
        self.UUID_TX = char  # the serial wrapper ignores the char on write

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except Exception:
            pass
        await super().disconnect()

    async def fetch_voltages(self) -> List[int]:
        if not self._last_cells:
            await self.fetch()
        return self._last_cells

    async def fetch_temperatures(self) -> List[float]:
        if not self._last_temps:
            await self.fetch()
        return self._last_temps
