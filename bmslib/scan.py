import asyncio
import time
from functools import partial
from typing import Tuple, Dict

from bleak import BleakScanner

from bmslib.util import get_logger

_scanners: Dict[str, Tuple[BleakScanner, float]] = {}

_stop_task: asyncio.Task = None

lock = asyncio.Lock()

SCANNER_TIMEOUT = 30  # seconds

logger = get_logger()


async def get_shared_scanner(adapter=None, restart=False, **kwargs) -> BleakScanner:
    global _stop_task
    async with lock:
        if adapter not in _scanners:
            sc = BleakScanner(adapter=adapter, **kwargs) if adapter else BleakScanner(**kwargs)

            async def _stop(self, stop):
                await stop()
                self._stopped = True

            sc.stop = partial(_stop, sc, sc.stop)
            _scanners[adapter] = sc, time.time()
            await sc.start()
            logger.debug('scanner started %s %s', adapter, sc)
            if _stop_task is None or _stop_task.done():
                _stop_task = asyncio.create_task(_stop_loop())
        elif restart:
            sc = _scanners[adapter][0]
            logger.info(f'restarting scanner on adapter {adapter}')
            await sc.stop()
            await asyncio.sleep(.2)
            await sc.start()

        _scanners[adapter] = _scanners[adapter][0], time.time()
        return _scanners[adapter][0]


async def _stop_loop():
    while _scanners:
        async with lock:
            now = time.time()
            for adapter, (sc, t_last_use) in _scanners.copy().items():
                if now - t_last_use > SCANNER_TIMEOUT or (hasattr(sc, "_stopped") and sc._stopped):
                    logger.debug('stopping scanner %s %s', adapter, sc)
                    try:
                        await sc.stop()
                    except Exception as e:
                        print('error stopping scanner', adapter, sc, e)
                    _scanners.pop(adapter)

        await asyncio.sleep(1)
