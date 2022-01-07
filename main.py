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
        level = logging.WARNING

    logging.basicConfig(level=level, format=log_format, datefmt='%H:%M:%S')
    return logging.getLogger()


async def bt_discovery():
    print('BT Discovery:')

    devices = await BleakScanner.discover()
    for d in devices:
        print(d)


async def main():
    logger = get_logger(verbose=False)
    bms = DalyBMSBluetooth(request_retries=3, logger=logger)

    print('connecting mqtt %s@%s' % (user_config.mqtt_user, user_config.mqtt_broker))
    mqtt_client = paho.Client()
    mqtt_client.enable_logger(logger)
    if user_config.get('mqtt_user', None):
        mqtt_client.username_pw_set(user_config.mqtt_user, user_config.mqtt_password)
    mqtt_client.connect(user_config.mqtt_broker, port=1883)

    num_errors = 0
    while True:
        try:
            print('connecting bms')
            t_conn = time.time()
            await bms.connect(mac_address=mac_address)
            print('fetching data')
            t_fetch = time.time()
            result = await bms.get_all()
            print('result@', datetime.datetime.now().isoformat(), result)
            t_disc = time.time()
            await bms.disconnect()
            mqtt_iterator(mqtt_client, result=result, topic='daly_bms', hass=True)

            print('times: connect=%.2fs fetch=%.2fs' % (t_fetch - t_conn, t_disc - t_fetch))

            await asyncio.sleep(8)
        except Exception as e:
            logger.error('Error fetching BMS: %s', e)

            if num_errors == 0:
                await bt_discovery()

            num_errors += 1

            await asyncio.sleep(30)


asyncio.run(main())
