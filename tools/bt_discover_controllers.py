import asyncio
import os

from bmslib.ble.bt import bt_discovery
from bmslib.util import get_logger

logger = get_logger()

# ls -lA /sys/class/bluetooth/
adapters = os.listdir('/sys/class/bluetooth')

print(adapters)

async def main():
    for bl_ctrl in adapters: # TODO find by hcitool dev
        await bt_discovery(logger, timeout=5, adapter=bl_ctrl)


asyncio.run(main())
