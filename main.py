import asyncio
import datetime
import json
import sys
import time
import traceback
from typing import List

import paho.mqtt.client as paho
from bleak import BleakScanner

import victron
from mqtt_util import mqtt_iterator, publish_hass_discovery, publish_sample, publish_cell_voltages
from util import dotdict, get_logger

try:
    with open('/data/options.json') as f:
        user_config = dotdict(json.load(f))
except Exception as e:
    print('error reading /data/options.json', e)
    user_config = dotdict(
        daly_address=sys.argv[1],
        jbd_address=sys.argv[2],
        victron_address=sys.argv[3],
        mqtt_broker='homeassistant.local',
        mqtt_user='pv',
        mqtt_password='0ffgrid',
    )

mac_address = user_config.get('daly_address')


async def bt_discovery():
    print('BT Discovery:')

    devices = await BleakScanner.discover()
    for d in devices:
        logger.info("BT Device: %s", d)


logger = get_logger(verbose=False)

import bmslib.daly
import bmslib.jbd
import bmslib.bt


async def main():

    bms_list: List[bmslib.bt.BtBms] = []

    if user_config.get('daly_address'):
        bms_list.append(bmslib.daly.DalyBt(user_config.get('daly_address'), name='daly_bms'))

    if user_config.get('jbd_address'):
        bms_list.append(bmslib.jbd.JbdBt(user_config.get('jbd_address'), name='jbd_bms'))


    logger.info('connecting mqtt %s@%s', user_config.mqtt_user, user_config.mqtt_broker)
    mqtt_client = paho.Client()
    mqtt_client.enable_logger(logger)
    if user_config.get('mqtt_user', None):
        mqtt_client.username_pw_set(user_config.mqtt_user, user_config.mqtt_password)
    mqtt_client.connect(user_config.mqtt_broker, port=1883)

    for bms in bms_list:
        publish_hass_discovery(mqtt_client, device_topic=bms.name, num_cells=8)

    discovered = False
    num_errors_row = 0
    while True:
        for bms in bms_list:
            try:
                logger.info('connecting bms %s', bms)
                t_conn = time.time()
                await bms.connect()

                t_fetch = time.time()
                sample = await bms.fetch()
                logger.info('result@%s %s', datetime.datetime.now().isoformat(), sample)
                publish_sample(mqtt_client, device_topic=bms.name, sample=sample)

                voltages = await bms.fetch_voltages()
                logger.info('Voltages: %s', voltages)
                publish_cell_voltages(mqtt_client, device_topic=bms.name, voltages=voltages)


                t_disc = time.time()
                await bms.disconnect()

                # mqtt_iterator(mqtt_client, result=result, topic='daly_bms', hass=True)

                logger.info('bms times: connect=%.2fs fetch=%.2fs', t_fetch - t_conn, t_disc - t_fetch)


            except Exception as e:
                num_errors_row += 1

                logger.error('Error (num %d) reading BMS: %s', num_errors_row, e)
                logger.error('Stack: %s', traceback.format_exc())

                if not discovered:
                    await bt_discovery()
                    discovered = True

                if num_errors_row > 4:
                    print('too many errors, abort')
                    break

                await asyncio.sleep(30)

        if user_config.get('victron_address'):
            result = await victron.fetch_device(user_config.get('victron_address'))
            mqtt_iterator(mqtt_client, result=result, topic='victron_shunt1', hass=True)

        num_errors_row = 0
        await asyncio.sleep(6)


asyncio.run(main())

exit(1)
