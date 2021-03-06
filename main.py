import asyncio
import atexit
import json
import random
import signal
import sys
import traceback
from typing import List

import paho.mqtt.client as paho
from bleak import BleakScanner

import bmslib.bt
import bmslib.daly
import bmslib.jbd
from bmslib.sampling import BmsSampler
from mqtt_util import mqtt_iterator
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
logger = get_logger(verbose=False)
shutdown = False


async def bt_discovery():
    logger.info('BT Discovery:')
    devices = await BleakScanner.discover()
    if not devices:
        logger.info(' - no devices found - ')
    for d in devices:
        logger.info("BT Device   %s   address=%s", d.name, d.address)
    return devices


async def fetch_loop(fn, period, max_errors=4):
    num_errors_row = 0
    while not shutdown:
        try:
            await fn()
            num_errors_row = 0
        except Exception as e:
            num_errors_row += 1
            logger.error('Error (num %d) reading BMS: %s', num_errors_row, e)
            logger.error('Stack: %s', traceback.format_exc())
            if num_errors_row > max_errors:
                logger.warning('too many errors, abort')
                break
        await asyncio.sleep(period)


async def main():
    bms_list: List[bmslib.bt.BtBms] = []
    extra_tasks = []

    try:
        devices = await bt_discovery()
    except Exception as e:
        devices = []
        logger.error('Error discovering devices: %s', e)

    def dev2addr(name: str):
        return next((d.address for d in devices if d.name.strip() == name.strip()), name)

    verbose_log = user_config.get('verbose_log', False)
    if verbose_log:
        logger.info('Verbose logging enabled')

    bms_registry = dict(
        daly=bmslib.daly.DalyBt,
        jbd=bmslib.jbd.JbdBt,
    )
    for slug, bms_class in bms_registry.items():
        addr: str = user_config.get(f'{slug}_address')
        if addr and not addr.startswith('#'):
            bms_debug = addr.endswith('?')
            if bms_debug:
                logger.info('Verbose log for %s enabled', addr)
            addr = dev2addr(addr[:-1] if bms_debug else addr)
            bms_list.append(bms_class(addr, name='%s_bms' % slug, verbose_log=verbose_log or bms_debug))

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

    sampler_list = [BmsSampler(bms, mqtt_client=mqtt_client, dt_max=4) for bms in bms_list]

    sample_period = float(user_config.get('sample_period', 1.0))
    parallel_fetch = user_config.get('concurrent_sampling', False)
    if parallel_fetch:
        # parallel_fetch now uses a loop for each BMS so they don't delay each other
        tasks = sampler_list + extra_tasks

        # before we start the loops connect to each bms
        for t in tasks:
            try:
                await t()
            except:
                pass

        loops = [asyncio.create_task(fetch_loop(fn, period=sample_period)) for fn in tasks]
        await asyncio.wait(loops, return_when='FIRST_COMPLETED')

    else:
        async def fn():
            tasks = ([smp() for smp in sampler_list] + [t() for t in extra_tasks])

            if parallel_fetch:
                # concurrent synchronised fetch
                # this branch is currently not reachable!
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

        await fetch_loop(fn, period=sample_period)

    global shutdown
    shutdown = True

    for bms in bms_list:
        try:
            logger.info("Disconnecting %s", bms)
            await bms.disconnect()
        except:
            pass


def on_exit(*args, **kwargs):
    global shutdown
    logger.info('exit signal handler...')
    shutdown = True


atexit.register(on_exit)
# noinspection PyTypeChecker
signal.signal(signal.SIGTERM, on_exit)
# noinspection PyTypeChecker
signal.signal(signal.SIGINT, on_exit)

asyncio.run(main())
exit(1)
