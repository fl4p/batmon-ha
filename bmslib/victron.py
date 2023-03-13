"""
 https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html
 https://github.com/Fabian-Schmidt/esphome-victron_ble
"""
import asyncio
import math
from functools import partial
from typing import Optional

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


VICTRON_CHARACTERISTICS = {
    "charge": dict( # consumend Ah
        uuid="6597eeff-4bda-4c1e-af4b-551c4cf74769",
        unit='Ah',
        func=lambda b: int.from_bytes(b, 'little', signed=True) * .1,
        na_bytes=b'\xff\xff\xff\x7f',
    ),
    "power": dict(
        uuid="6597ed8e-4bda-4c1e-af4b-551c4cf74769",
        unit='W',
        func=lambda b: -int.from_bytes(b, 'little', signed=True)
    ),
    "voltage": dict(
        uuid="6597ed8d-4bda-4c1e-af4b-551c4cf74769",
        unit='V',
        func=lambda b: int.from_bytes(b, 'little', signed=True) * .01
    ),
    "current": dict(
        uuid="6597ed8c-4bda-4c1e-af4b-551c4cf74769",
        unit='A',
        func=lambda b: -int.from_bytes(b, 'little', signed=True) * .001
    ),
    "soc": dict(
        uuid="65970fff-4bda-4c1e-af4b-551c4cf74769",
        unit='%',
        func=lambda b: int.from_bytes(b, 'little', signed=False) * .01,
        na_bytes=b'\xff\xff',
    )
}

def parse_value(data, char):
    return char['func'](data) if data != char.get('na_bytes') else math.nan

class SmartShuntBt(BtBms):
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._keep_alive_task: Optional[asyncio.Task] = None
        self._values = {}

    async def _keep_alive_loop(self):
        interval = 60_000
        data = interval.to_bytes(length=2, byteorder="little", signed=False)
        while True:
            self.logger.debug('write keep_alive %s', data)
            await self.client.write_gatt_char('6597ffff-4bda-4c1e-af4b-551c4cf74769', data, response=False)
            await asyncio.sleep(interval / 1000 / 2)

    async def connect(self, timeout=20):
        await super().connect(timeout=timeout)
        self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())
        for k,char in VICTRON_CHARACTERISTICS.items():
            self._values[k] = parse_value(await self.client.read_gatt_char(char['uuid']), char)
            await self.client.start_notify(char['uuid'], partial(self._handle_notification, k))

    async def disconnect(self):
        self._keep_alive_task.cancel()
        for k,char in VICTRON_CHARACTERISTICS.items():
            try:
                await self.client.stop_notify(char['uuid'])
            except:
                pass
        await super().disconnect()

    def _handle_notification(self, key, sender, data):
        val = parse_value(data, VICTRON_CHARACTERISTICS[key])
        self._values[key] = val
        self.logger.debug('msg %s %s', key, val)

    async def fetch(self) -> BmsSample:
        values = self._values
        sample = BmsSample(**values)
        return sample

    async def fetch_voltages(self):
        return []

    async def fetch_temperatures(self):
        return []


async def main():
    #raise NotImplementedError()
    v = SmartShuntBt(address='95E605C8-E9DC-DD43-E368-D9B1DA8301B7', name='test')
    await v.connect()

    _prev_val = None

    while True:
        r = await v.fetch()
        v.logger.info(r)
        await asyncio.sleep(2)

        # testing read vs notify latency and frequency:
        #char = VICTRON_CHARACTERISTICS['current']
        #val = parse_value(await v.client.read_gatt_char(char['uuid']), char)
        #if val != _prev_val:
        #    print('val changed', val)
        #    _prev_val = val
        #await asyncio.sleep(.1)


if __name__ == '__main__':
    asyncio.run(main())
