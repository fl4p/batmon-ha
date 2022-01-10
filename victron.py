import math

from bleak import BleakClient

from util import get_logger

logger = get_logger()

victron_chars = {
    "charge": dict(
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
        func=lambda b: int.from_bytes(b, 'little', signed=True) * .001
    ),
    "soc": dict(
        uuid="65970fff-4bda-4c1e-af4b-551c4cf74769",
        unit='%',
        func=lambda b: int.from_bytes(b, 'little', signed=False) * .01,
        na_bytes=b'\xff\xff',
    )
}


async def fetch_device(mac_address):
    async with BleakClient(mac_address) as client:
        logger.info(f"Connected: {client.is_connected}")

        ret = {}

        for field_name, char in victron_chars.items():
            data = await client.read_gatt_char(char['uuid'])
            val = char['func'](data) if data != char.get('na_bytes') else math.nan
            # print('%10s = %8.3f %s' % (field_name, val, char['unit']))
            ret[field_name] = val

        return ret
