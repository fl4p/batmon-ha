import math
from typing import Union

# import dbus
# import dbus.service
from bleak import BleakClient

from bmslib.util import get_logger

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


async def fetch_device(address, psk: str = None):
    try:
        import bleak.backends.bluezdbus.agent
    except ImportError:
        logger.warn("this bleak version has no pairing agent, pairing with a pin will probably fail!")

    def get_passkey(device: str, pin, passkey):

        if pin:
            logger.info(f"Device {device} is displaying pin '{pin}'")
            return True

        if passkey:
            logger.info(f"Device {device} is displaying passkey '{passkey:06d}'")
            return True

        logger.info(f"Device {device} asking for psk, giving '{pin}'")

        return str(psk) or None

    logger.info('Connecting %s to pair', address)
    async with BleakClient(address, handle_pairing=bool(psk)) as client:
        if psk:
            logger.info("Pairing %s using psk '%s'...", address, psk)
            res = await client.pair(callback=get_passkey)
            logger.info("Paired %s: %s", address, res)

        ret = {}

        for field_name, char in victron_chars.items():
            data = await client.read_gatt_char(char['uuid'])
            val = char['func'](data) if data != char.get('na_bytes') else math.nan
            # print('%10s = %8.3f %s' % (field_name, val, char['unit']))
            ret[field_name] = val

        return ret


def get_passkey(
        device: str, pin: Union[None, str], passkey: Union[None, int]
) -> Union[bool, int, str, None]:
    print('get_passkey', device)

    if pin:
        print(f"Device {device} is displaying pin '{pin}'")
        return True

    if passkey:
        print(f"Device {device} is displaying passkey '{passkey:06d}'")
        return True

    # Retrieve passkey using custom algorithm, web API or just ask the user like OS pairing
    # wizard would do
    psk = input(
        f"Provide pin (1-16 characters) or passkey (0-999999) for {device}, or nothing to reject "
        f"pairing: "
    )

    # Return None if psk is empty string (pincode 0 is valid pin, but "0" is True)
    return psk or None


if __name__ == "__main__":
    import asyncio

    import logging


    def setup_logger():
        # create logger
        logger = logging.getLogger('project')
        logger.setLevel(logging.DEBUG)

        # create console handler and set level to debug
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)

        # create formatter
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')

        # add formatter to ch
        ch.setFormatter(formatter)

        # add ch to logger
        logger.addHandler(ch)


    setup_logger()


    async def main():
        logging.getLogger('bleak.backends.corebluetooth.client').setLevel(logging.DEBUG)
        # logging.getLogger('bleak.backends.bluezdbus.client').setLevel(logging.DEBUG)
        # logging.getLogger('bleak.backends.bluezdbus.client').debug('TEsT debug msg')
        # devices = await BleakScanner.discover()
        # for d in devices:
        #    logger.info("BT Device: %s", d)

        mac_address = '980B4D75-B14B-4871-9DA4-D0B440CD3D85'
        mac_address = 'E0:E5:16:A0:5A:C8'

        # r = await fetch_device(mac_address)
        # print('fetch_device', r)

        async with BleakClient(mac_address, handle_pairing=True) as client:
            logger.info('connected  %s', mac_address)
            print("Pairing...")
            print(await client.pair(callback=get_passkey))
            print("Paired")

        ret = await fetch_device(mac_address)
        print('result', ret)


    asyncio.run(main())
