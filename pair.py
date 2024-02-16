# pairs devices that with a PSK/pin such as the victron smartshunt
# using bleak version git+https://github.com/jpeters-ml/bleak@feature/windowsPairing
import asyncio
import sys
from typing import Dict

import bmslib
from bmslib.bt import bt_discovery
from bmslib.models import get_bms_model_class, construct_bms
from bmslib.store import load_user_config
from bmslib.util import get_logger

logger = get_logger()

user_config: Dict[str, any] = load_user_config()
verbose_log = user_config.get('verbose_log', False)


async def main():
    # try:
    #    devices = await asyncio.wait_for(bt_discovery(logger), 30)
    # except Exception as e:
    #    devices = []
    #    logger.error('Error discovering devices: %s', e)

    devices = []

    for dev in user_config.get('devices', []):

        if not dev.get('pin'):
            continue

        addr: str = dev['address']

        if not addr or addr.startswith('#'):
            continue

        bms_class = get_bms_model_class(dev['type'])

        if bms_class is None:
            logger.warning('Unknown device type %s', dev)
            continue

        bms = construct_bms(dev, verbose_log=verbose_log, bt_discovered_devices=devices)

        logger.info('connecting to pair %s', bms)
        await bms._connect_client(timeout=10)
        await bms.disconnect()
