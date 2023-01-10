import asyncio
import atexit
import json
import random
import signal
import time
import traceback
from functools import partial
from typing import List

import paho.mqtt.client as paho
from bleak import BleakScanner

import bmslib.bt
import bmslib.daly
import bmslib.jbd
import bmslib.jikong
import bmslib.dummy
from bmslib.bms import MIN_VALUE_EXPIRY
from bmslib.sampling import BmsSampler
from bmslib.util import dotdict, get_logger
from mqtt_util import mqtt_iterator_victron, mqqt_last_publish_time, mqtt_message_handler, mqtt_process_action_queue


def load_user_config():
    try:
        with open('/data/options.json') as f:
            conf = dotdict(json.load(f))
            _user_config_migrate_addresses(conf)
    except Exception as e:
        print('error reading /data/options.json, trying options.json', e)
        with open('options.json') as f:
            conf = dotdict(json.load(f))
    return conf


def _user_config_migrate_addresses(conf):
    changed = False
    slugs = ["daly", "jbd", "jk", "victron"]
    conf["devices"] = conf.get('devices') or []
    devices_by_address = {d['address']: d for d in conf["devices"]}
    for slug in slugs:
        addr = conf.get(f'{slug}_address')
        if addr and not devices_by_address.get(addr):
            device = dict(
                address=addr.strip('?'),
                type=slug,
                alias=slug + '_bms',
            )
            if addr.endswith('?'):
                device["debug"] = True
            if conf.get(f'{slug}_pin'):
                device['pin'] = conf.get(f'{slug}_pin')
            conf["devices"].append(device)
            del conf[f'{slug}_address']
            logger.info('Migrated %s_address to device %s', slug, device)
            changed = True
    if changed:
        logger.info('Please update add-on configuration manually.')


logger = get_logger(verbose=False)
user_config = load_user_config()
shutdown = False


async def bt_discovery():
    logger.info('BT Discovery:')
    devices = await BleakScanner.discover()
    if not devices:
        logger.info(' - no devices found - ')
    for d in devices:
        logger.info("BT Device   %s   address=%s", d.name, d.address)
    return devices


async def fetch_loop(fn, period, max_errors):
    num_errors_row = 0
    while not shutdown:
        try:
            await fn()
            num_errors_row = 0
        except Exception as e:
            num_errors_row += 1
            logger.error('Error (num %d) reading BMS: %s', num_errors_row, e)
            logger.error('Stack: %s', traceback.format_exc())
            if max_errors and num_errors_row > max_errors:
                logger.warning('too many errors, abort')
                break
        await asyncio.sleep(period)


async def background_loop(timeout: float):
    global shutdown

    t_start = time.time()

    if timeout:
        logger.info("mqtt watchdog loop started with timeout %.1fs", timeout)

    while not shutdown:

        await mqtt_process_action_queue()

        if timeout:
            # compute time since last successful publish
            pdt = time.time() - (mqqt_last_publish_time() or t_start)
            if pdt > timeout:
                if mqqt_last_publish_time():
                    logger.error("MQTT message publish timeout (last %.0fs ago), exit", pdt)
                else:
                    logger.error("MQTT never published a message after %.0fs, exit", timeout)
                shutdown = True
                break
            await asyncio.sleep(.1)


async def main():
    bms_list: List[bmslib.bt.BtBms] = []
    extra_tasks = []

    try:
        devices = await bt_discovery()
    except Exception as e:
        devices = []
        logger.error('Error discovering devices: %s', e)

    def name2addr(name: str):
        return next((d.address for d in devices if d.name.strip() == name.strip()), name)

    def dev_by_addr(address: str):
        return next((d for d in devices if d.address == address), None)

    verbose_log = user_config.get('verbose_log', False)
    if verbose_log:
        logger.info('Verbose logging enabled')

    bms_registry = dict(
        daly=bmslib.daly.DalyBt,
        jbd=bmslib.jbd.JbdBt,
        jk=bmslib.jikong.JKBt,
        dummy=bmslib.dummy.DummyBt,
    )

    async def _fetch_victron(dev):
        result = await victron.fetch_device(dev['address'], psk=dev.get('pin'))
        mqtt_iterator_victron(mqtt_client, result=result, topic=dev['alias'], hass=True)

    names = set()

    for dev in user_config.get('devices', []):
        addr: str = dev['address']
        if addr and not addr.startswith('#'):
            if dev['type'] in bms_registry:
                bms_class = bms_registry[dev['type']]
                if dev.get('debug'):
                    logger.info('Verbose log for %s enabled', addr)
                addr = name2addr(addr)
                name: str = dev.get('alias') or dev_by_addr(addr).name
                assert name not in names, "duplicate name %s" % name
                bms_list.append(bms_class(addr, name=name, verbose_log=verbose_log or dev.get('debug')))
                names.add(name)
            elif dev['type'] == 'victron':
                import victron
                if dev.get('pin'):
                    try:
                        r = await victron.fetch_device(user_config.get('victron_address'), psk=dev.get('pin'))
                        logger.info("Victron: %s", r)
                        r = await victron.fetch_device(user_config.get('victron_address'))
                        logger.info("Victron2: %s", r)
                    except Exception as e:
                        logger.error('Error pairing victron device: %s', e)
                extra_tasks.append(partial(_fetch_victron, dev))

    for bms in bms_list:
        bms.set_keep_alive(user_config.get('keep_alive', False))

    logger.info('connecting mqtt %s@%s', user_config.mqtt_user, user_config.mqtt_broker)
    mqtt_client = paho.Client()
    mqtt_client.enable_logger(logger)
    if user_config.get('mqtt_user', None):
        mqtt_client.username_pw_set(user_config.mqtt_user, user_config.mqtt_password)

    mqtt_client.on_message = mqtt_message_handler

    try:
        mqtt_client.connect(user_config.mqtt_broker, port=user_config.get('mqtt_port', 1883))
        mqtt_client.loop_start()
    except Exception as ex:
        logger.error('mqtt connection error %s', ex)

    sample_period = float(user_config.get('sample_period', 1.0))
    expire_values_after = float(user_config.get('expire_values_after', MIN_VALUE_EXPIRY))
    ic = user_config.get('invert_current', False)
    sampler_list = [BmsSampler(bms, mqtt_client=mqtt_client,
                               dt_max=4,
                               expire_after_seconds=max(expire_values_after, int(sample_period * 2 + .5)),
                               invert_current=ic) for bms in bms_list]

    parallel_fetch = user_config.get('concurrent_sampling', False)

    logger.info('Fetching %d BMS + %d others %s, period=%.2fs, keep_alive=%s', len(sampler_list), len(extra_tasks),
                'concurrently' if parallel_fetch else 'serially', sample_period, user_config.get('keep_alive', False))

    watchdog_en = user_config.get('watchdog', False)
    max_errors = 200 if watchdog_en else 0

    asyncio.create_task(background_loop(timeout=max(120., sample_period * 3) if watchdog_en else 0))

    if parallel_fetch:
        # parallel_fetch now uses a loop for each BMS so they don't delay each other
        tasks = sampler_list + extra_tasks

        # before we start the loops connect to each bms
        for t in tasks:
            try:
                await t()
            except:
                pass

        loops = [asyncio.create_task(fetch_loop(fn, period=sample_period, max_errors=max_errors)) for fn in tasks]
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

        await fetch_loop(fn, period=sample_period, max_errors=max_errors)


    global shutdown
    shutdown = True

    logger.info('Shutting down ...')
    # await asyncio.sleep(4)

    for bms in bms_list:
        try:
            logger.info("Disconnecting %s", bms)
            await bms.disconnect()
            # await asyncio.sleep(2)
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
