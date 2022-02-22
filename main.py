import asyncio
import datetime
import json
import random
import sys
import time
import traceback
from typing import List

import paho.mqtt.client as paho
from bleak import BleakScanner

import bmslib.bt
import bmslib.daly
import bmslib.jbd

from mqtt_util import mqtt_iterator, publish_hass_discovery, publish_sample, publish_cell_voltages, publish_temperatures
from util import dotdict, get_logger


def load_user_config():
    try:
        with open('/data/options.json') as f:
            conf = dotdict(json.load(f))
    except Exception as e:
        print('error reading /data/options.json', e)
        conf = dotdict(
            daly_address=sys.argv[1],
            jbd_address=sys.argv[2],
            victron_address=sys.argv[3],
            victron_pin='000000',
            mqtt_broker='homeassistant.local',
            mqtt_user='pv',
            mqtt_password='0ffgrid',
            concurrent_sampling=False,
            keep_alive=False,
            verbose_log=False,
        )
    return conf


user_config = load_user_config()


async def bt_discovery():
    logger.info('BT Discovery:')
    devices = await BleakScanner.discover()
    if not devices:
        logger.info(' - no devices found - ')
    for d in devices:
        logger.info("BT Device   %s   address=%s", d.name, d.address)
    return devices


logger = get_logger(verbose=False)


async def sample_bms(bms: bmslib.bt.BtBms, mqtt_client,):
    logger.info('connecting bms %s', bms)
    t_conn = time.time()

    try:
        async with bms:
            t_fetch = time.time()
            sample = await bms.fetch()
            publish_sample(mqtt_client, device_topic=bms.name, sample=sample)
            logger.info('%s result@%s %s', bms.name, datetime.datetime.now().isoformat(), sample)

            voltages = await bms.fetch_voltages()
            publish_cell_voltages(mqtt_client, device_topic=bms.name, voltages=voltages)
            logger.info('%s Voltages: %s', bms.name, voltages)

            temperatures = sample.temperatures or await bms.fetch_temperatures()
            publish_temperatures(mqtt_client, device_topic=bms.name, temperatures=temperatures)
            logger.info('%s Temperatures: %s', bms.name, temperatures)

            t_disc = time.time()
    except Exception as ex:
        logger.error('%s error: %s', bms.name, str(ex) or str(type(ex)))
        # logger.error('Stack: %s', traceback.format_exc())
        raise

    logger.info('%s times: connect=%.2fs fetch=%.2fs', bms, t_fetch - t_conn, t_disc - t_fetch)


async def main():
    bms_list: List[bmslib.bt.BtBms] = []
    extra_tasks = []

    try:
        devices = await bt_discovery()
    except Exception as e:
        devices = []
        logger.error('Error discovering devices: %s', e)

    def dev2addr(name:str):
        return next((d.address for d in devices if d.name.strip() == name.strip()), name)

    verbose_log = user_config.get('verbose_log', False)
    if verbose_log:
        logger.info('Verbose logging enabled')

    if user_config.get('daly_address'):
        bms_list.append(bmslib.daly.DalyBt(dev2addr(user_config.get('daly_address')), name='daly_bms', verbose_log=verbose_log))

    if user_config.get('jbd_address'):
        bms_list.append(bmslib.jbd.JbdBt(dev2addr(user_config.get('jbd_address')), name='jbd_bms', verbose_log=verbose_log))

    for bms in bms_list:
        bms.set_keep_alive(user_config.get('keep_alive', False))

    if user_config.get('victron_address') and not user_config.get('victron_address').startswith('#'):
        import victron
        victron_pin = user_config.get('victron_pin', None)
        if victron_pin:
            try:
                r = await victron.fetch_device(user_config.get('victron_address'), psk=victron_pin)
                logger.info("Victron: %s", r)
                r = await victron.fetch_device(user_config.get('victron_address'))
                logger.info("Victron2: %s", r)
            except Exception as e:
                logger.error('Error pairing victron device: %s', e)

        async def _fetch_victron():
            result = await victron.fetch_device(user_config.get('victron_address'), psk=victron_pin)
            mqtt_iterator(mqtt_client, result=result, topic='victron_shunt1', hass=True)

        extra_tasks.append(_fetch_victron)

    logger.info('connecting mqtt %s@%s', user_config.mqtt_user, user_config.mqtt_broker)
    mqtt_client = paho.Client()
    mqtt_client.enable_logger(logger)
    if user_config.get('mqtt_user', None):
        mqtt_client.username_pw_set(user_config.mqtt_user, user_config.mqtt_password)

    try:
        mqtt_client.connect(user_config.mqtt_broker, port=1883)
    except Exception as ex:
        logger.error('mqtt connection error %s', ex)

    for bms in bms_list:
        publish_hass_discovery(mqtt_client, device_topic=bms.name, num_cells=8, num_temp_sensors=1)

    discovered = False
    num_errors_row = 0
    parallel_fetch = user_config.get('concurrent_sampling', False)

    while True:
        tasks = ([sample_bms(bms, mqtt_client) for bms in bms_list] + [t() for t in extra_tasks])

        try:
            if parallel_fetch:
                await asyncio.gather(*tasks, return_exceptions=False)
            else:
                random.shuffle(tasks)
                exceptions = []
                for t in tasks:
                    try:
                        await t
                    except Exception as ex:
                        exceptions.append(ex)
                if exceptions:
                    logger.error('%d exceptions occurred fetching BMSs', len(exceptions))
                    raise exceptions[0]

            num_errors_row = 0
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

        await asyncio.sleep(1)


asyncio.run(main())

exit(1)
