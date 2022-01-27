import asyncio

from bleak import BleakScanner

async def bt_discovery():
    print('BT Discovery:')
    devices = await BleakScanner.discover()
    for d in devices:
        print("BT Device: %s" % d)

asyncio.run(bt_discovery())