"""

"""
import asyncio
import math

from .bms import BmsSample
from .bt import BtBms

# reference: https://community.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html
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
        func=lambda b: int.from_bytes(b, 'little', signed=True)
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


class SmartShuntBt(BtBms):
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)

    async def fetch(self) -> BmsSample:
        values = {}
        for field_name, char in VICTRON_CHARACTERISTICS.items():
            data = await self.client.read_gatt_char(char['uuid'])
            val = char['func'](data) if data != char.get('na_bytes') else math.nan
            values[field_name] = val

        sample = BmsSample(**values)
        return sample

    async def fetch_voltages(self):
        return []

    async def fetch_temperatures(self):
        return []


async def main():
    raise NotImplementedError()


if __name__ == '__main__':
    asyncio.run(main())
