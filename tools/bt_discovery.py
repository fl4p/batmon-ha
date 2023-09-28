import asyncio
import logging

from bleak import BleakScanner

logger = logging.getLogger(__name__)

async def bt_discovery():
    logger.info('BT Discovery:')
    devices = await BleakScanner.discover()
    if not devices:
        logger.info(' - no devices found - ')
    for d in devices:
        logger.info("BT Device   %s   address=%s", d.name, d.address)
    return devices
    
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(bt_discovery())   
