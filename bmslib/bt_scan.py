import asyncio
import time
from typing import Dict, Callable, Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from bmslib.util import get_logger

logger = get_logger(False)


class BtSharedScanner():

    def __init__(self, adapter=None, **kwargs):
        self.scanner = BleakScanner(detection_callback=self._on_detection, adapter=adapter, **kwargs)
        self.callbacks = []
        self._scanning = False
        self.devices: Dict[str, BLEDevice] = {}
        self._stop_time = 0
        self._stop_task: asyncio.Task = None

    async def _on_detection(self, device: BLEDevice, advertisement_data):
        if not self._scanning:
            return

        now = time.time()
        new = device.address not in self.devices
        if new:
            self.devices[device.address] = device
            logger.debug('new bt device %s %s', device, advertisement_data)
        rm = []
        for i in range(len(self.callbacks)):
            cb, t_stop = self.callbacks[i]
            if t_stop >= now:
                if new:
                    cb(device, advertisement_data)
            else:
                rm.append(i)
        for i in rm:
            self.callbacks.pop(i)

        if now >= self._stop_time:
            await self._stop_wait()

    async def _stop_wait(self):
        while time.time() < self._stop_time:
            await asyncio.sleep(.2)
        if self._scanning:
            self._scanning = False
            await self.scanner.stop()
            logger.info('scanner stopped')

    def schedule_stop(self, delay):
        # if delay:
        self._stop_time = max(self._stop_time, time.time() + delay)

        if not self._stop_task or self._stop_task.done():
            self._stop_task = asyncio.create_task(self._stop_wait())

            def _discard(t):
                if t == self._stop_task:
                    self._stop_task = None

            self._stop_task.add_done_callback(_discard)

    async def start(self, on_discovered: Optional[Callable[[BLEDevice, AdvertisementData], None]] = None,
                    clear=False,
                    stop_after=5.0):
        bs = self.scanner

        if clear:
            self.devices.clear()

        if on_discovered:
            self.callbacks.append((on_discovered, time.time() + stop_after))

        if not self._scanning:
            await bs.start()
            self._scanning = True
            logger.info('started scanner, stop scheduled in %.1fs', stop_after)

        self.schedule_stop(stop_after)

    async def wait_for_device(self, address, timeout=5.0):
        if not self._scanning:
            await self.start(stop_after=timeout)
        for i in range(1, 10):
            if address in self.devices:
                d = self.devices[address]
                logger.info("device %s (%s, rssi=%2) detected after %.1fs", address, d.name,
                            d.rssi, i * timeout / 10)
                return True
            await asyncio.sleep(timeout / 10)
        logger.info("device %s not detected after %.1fs", address, timeout)
        return False


class BtSharedScannerCollection():

    def __init__(self):
        self.scanners: Dict[str, BtSharedScanner] = {}

    def get_scanner(self, adapter=None):
        if adapter not in self.scanners:
            self.scanners[adapter] = BtSharedScanner(adapter=adapter)
        return self.scanners[adapter]


if __name__ == "__main__":
    async def main():
        scan = BtSharedScanner()
        await scan.wait_for_device('8B133977-182C-62EE-8E81-41FF77969EE9')

        await asyncio.sleep(10)
        logger.info('END sleep')


    asyncio.run(main())
