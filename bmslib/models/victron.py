"""
 https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html
 https://github.com/Fabian-Schmidt/esphome-victron_ble
"""
import asyncio
import math
import time
from functools import partial
from typing import Optional

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms

VICTRON_CHARACTERISTICS = {
    "charge": dict(  # consumed Ah
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
        self._values_t = {k: 0 for k in VICTRON_CHARACTERISTICS.keys()}

    async def _keep_alive_loop(self):
        interval = 20_000
        data = interval.to_bytes(length=2, byteorder="little", signed=False)
        while True:
            self.logger.debug('write keep_alive %s', data)
            await self.client.write_gatt_char('6597ffff-4bda-4c1e-af4b-551c4cf74769', data, response=False)
            await asyncio.sleep(interval / 1000 / 2)

    async def _subscribe(self, key):
        char = VICTRON_CHARACTERISTICS[key]
        self._values[key] = parse_value(await self.client.read_gatt_char(char['uuid']), char)
        self._values_t[key] = time.time()
        await self.start_notify(char['uuid'], partial(self._handle_notification, key))

    async def connect(self, timeout=20):
        await super().connect(timeout=timeout)
        self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())
        for k, char in VICTRON_CHARACTERISTICS.items():
            await self._subscribe(k)

    async def disconnect(self):
        self._keep_alive_task.cancel()
        for k, char in VICTRON_CHARACTERISTICS.items():
            try:
                await self.client.stop_notify(char['uuid'])
            except:
                pass
        await super().disconnect()

    def _handle_notification(self, key, sender, data):
        val = parse_value(data, VICTRON_CHARACTERISTICS[key])
        self._values[key] = val
        self._values_t[key] = time.time()
        self.logger.debug('msg %s %s', key, val)

    async def fetch(self) -> BmsSample:
        t_expire = time.time() - 10
        for k, t in self._values_t.items():
            if t < t_expire and not math.isnan(self._values.get(k, 0)):
                self.logger.warning('value for %s expired %s, re-sub', k, t)
                await self._subscribe(k)
        values = self._values
        sample = BmsSample(**values, timestamp=max(v for k, v in self._values_t.items() if not math.isnan(values[k])))
        return sample

    async def fetch_voltages(self):
        return []

    async def fetch_temperatures(self):
        return []

    async def fetch_device_info(self) -> DeviceInfo:
        dev = DeviceInfo(
            mnf="Victron",
            model='SmartShunt',
            hw_version=None,
            sw_version=None,
            name=None,
            sn=None,
        )
        return dev


async def main():
    # raise NotImplementedError()
    v = SmartShuntBt(address='95E605C8-E9DC-DD43-E368-D9B1DA8301B7', name='test')
    await v.connect()

    _prev_val = None

    while True:
        r = await v.fetch()
        v.logger.info(r)
        await asyncio.sleep(2)

        # testing read vs notify latency and frequency:
        # char = VICTRON_CHARACTERISTICS['current']
        # val = parse_value(await v.client.read_gatt_char(char['uuid']), char)
        # if val != _prev_val:
        #    print('val changed', val)
        #    _prev_val = val
        # await asyncio.sleep(.1)


if __name__ == '__main__':
    asyncio.run(main())
