import asyncio
import datetime
import json
import logging
import sys
import time

import paho.mqtt.client as paho
from bleak import BleakScanner

from daly_bms_bluetooth import DalyBMSBluetooth
from mqtt_util import mqtt_iterator
from util import dotdict

try:
    with open('/data/options.json') as f:
        user_config = dotdict(json.load(f))
except Exception as e:
    print('error reading /data/options.json', e)
    user_config = dotdict(
        daly_address=sys.argv[1],
        mqtt_broker='homeassistant.local',
        mqtt_user='pv',
        mqtt_password='0ffgrid',
    )

mac_address = user_config.get('daly_address')


def get_logger(verbose=False):
    log_format = '%(levelname)-8s [%(filename)s:%(lineno)d] %(message)s'
    if verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(level=level, format=log_format, datefmt='%H:%M:%S')
    return logging.getLogger()


async def bt_discovery():
    print('BT Discovery:')

    devices = await BleakScanner.discover()
    for d in devices:
        print(d)


logger = get_logger(verbose=False)


async def main():
    bms = DalyBMSBluetooth(request_retries=3, logger=logger)

    logger.info('connecting mqtt %s@%s', user_config.mqtt_user, user_config.mqtt_broker)
    mqtt_client = paho.Client()
    mqtt_client.enable_logger(logger)
    if user_config.get('mqtt_user', None):
        mqtt_client.username_pw_set(user_config.mqtt_user, user_config.mqtt_password)
    mqtt_client.connect(user_config.mqtt_broker, port=1883)

    discovered = False
    num_errors_row = 0
    while True:
        try:
            logger.info('connecting bms %s', mac_address)
            t_conn = time.time()
            await bms.connect(mac_address=mac_address)
            # print('fetching data')
            t_fetch = time.time()
            result = await bms.get_all()
            logger.info('result@%s %s', datetime.datetime.now().isoformat(), result)
            t_disc = time.time()
            await bms.disconnect()
            mqtt_iterator(mqtt_client, result=result, topic='daly_bms', hass=True)

            logger.info('times: connect=%.2fs fetch=%.2fs', t_fetch - t_conn, t_disc - t_fetch)
            num_errors_row = 0
            await asyncio.sleep(8)

        except Exception as e:
            num_errors_row += 1

            logger.error('Error (num %d) fetching BMS: %s', num_errors_row, e)

            if not discovered:
                await bt_discovery()
                discovered = True

            if num_errors_row > 4:
                print('too many errors, abort')
                break

            await asyncio.sleep(30)


asyncio.run(main())

exit(1)
