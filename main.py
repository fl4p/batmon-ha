import asyncio
import atexit
import random
import signal
import sys
import time
import traceback
from typing import List, Dict

import paho.mqtt.client as paho

import bmslib.bt
import bmslib.models.ant
import bmslib.models.daly
import bmslib.models.dummy
import bmslib.models.jbd
import bmslib.models.jikong
import bmslib.models.supervolt
import bmslib.models.victron
import mqtt_util
from bmslib.bms import MIN_VALUE_EXPIRY
from bmslib.group import VirtualGroupBms, BmsGroup
from bmslib.sampling import BmsSampler
from bmslib.store import load_user_config
from bmslib.util import get_logger
from mqtt_util import mqtt_last_publish_time, mqtt_message_handler, mqtt_process_action_queue

logger = get_logger(verbose=False)
user_config = load_user_config()
shutdown = False


async def fetch_loop(fn, period, max_errors):
    num_errors_row = 0
    while not shutdown:
        try:
            await fn()
            num_errors_row = 0
        except Exception as e:
            num_errors_row += 1
            logger.error('Error (num %d, max %d) reading BMS: %s', num_errors_row, max_errors, e)
            logger.error('Stack: %s', traceback.format_exc())
            if max_errors and num_errors_row > max_errors:
                logger.warning('too many errors, abort')
                break
        await asyncio.sleep(period)
    logger.info("fetch_loop %s ends", fn)


def store_states(samplers: List[BmsSampler]):
    meter_states = {s.bms.name: s.get_meter_state() for s in samplers}
    from bmslib.store import store_meter_states
    store_meter_states(meter_states)


async def background_loop(timeout: float, sampler_list: List[BmsSampler]):
    global shutdown

    t_start = time.time()
    t_last_store = t_start

    if timeout:
        logger.info("mqtt watchdog loop started with timeout %.1fs", timeout)

    while not shutdown:

        await mqtt_process_action_queue()
        now = time.time()

        if timeout:
            # compute time since last successful publish
            pdt = now - (mqtt_last_publish_time() or t_start)
            if pdt > timeout:
                if mqtt_last_publish_time():
                    logger.error("MQTT message publish timeout (last %.0fs ago), exit", pdt)
                else:
                    logger.error("MQTT never published a message after %.0fs, exit", timeout)
                shutdown = True
                break

        if now - t_last_store > 10:
            t_last_store = now
            try:
                store_states(sampler_list)
            except Exception as e:
                logger.error('Error starting states: %s', e)

        await asyncio.sleep(.1)


async def main():
    bms_list: List[bmslib.bt.BtBms] = []
    extra_tasks = []

    if user_config.get('bt_power_cycle'):
        try:
            logger.info('Power cycle bluetooth hardware')
            bmslib.bt.bt_power(False)
            await asyncio.sleep(1)
            bmslib.bt.bt_power(True)
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("Error power cycling BT: %s", e)

    try:
        if len(sys.argv) > 1 and sys.argv[1] == "skip-discovery":
            raise Exception("skip-discovery")
        devices = await bmslib.bt.bt_discovery(logger)
    except Exception as e:
        devices = []
        logger.error('Error discovering devices: %s', e)

    def name2addr(name: str):
        return next((d.address for d in devices if (d.name or "").strip() == name.strip()), name)

    def dev_by_addr(address: str):
        dev = next((d for d in devices if d.address == address), None)
        if not dev:
            raise Exception("Can't resolve device name %s, not discovered" % address)
        return dev

    verbose_log = user_config.get('verbose_log', False)
    if verbose_log:
        logger.info('Verbose logging enabled')

    logger.info('Bleak version %s, BtBackend version %s', bmslib.bt.bleak_version(), bmslib.bt.bt_stack_version())

    bms_registry = dict(
        daly=bmslib.models.daly.DalyBt,
        jbd=bmslib.models.jbd.JbdBt,
        jk=bmslib.models.jikong.JKBt,
        ant=bmslib.models.ant.AntBt,
        victron=bmslib.models.victron.SmartShuntBt,
        group_parallel=bmslib.group.VirtualGroupBms,
        # group_serial=bmslib.group.VirtualGroupBms, # TODO
        supervolt=bmslib.models.supervolt.SuperVoltBt,
        dummy=bmslib.models.dummy.DummyBt,
    )

    names = set()
    dev_args: Dict[str, dict] = {}

    for dev in user_config.get('devices', []):
        addr: str = dev['address']

        if not addr or addr.startswith('#'):
            continue

        if dev['type'] not in bms_registry:
            logger.warning('Unknown device type %s', dev)
            continue

        bms_class = bms_registry[dev['type']]
        if dev.get('debug'):
            logger.info('Verbose log for %s enabled', addr)
        addr = name2addr(addr)
        name: str = dev.get('alias') or dev_by_addr(addr).name
        assert name not in names, "duplicate name %s" % name
        bms_list.append(bms_class(addr,
                                  name=name,
                                  verbose_log=verbose_log or dev.get('debug'),
                                  psk=dev.get('pin'),
                                  adapter=dev.get('adapter'),
                                  ))
        names.add(name)
        dev_args[name] = dev

    bms_by_name: Dict[str, bmslib.bt.BtBms] = {
        **{bms.address: bms for bms in bms_list if not isinstance(bms, VirtualGroupBms)},
        **{bms.name: bms for bms in bms_list}}
    groups_by_bms: Dict[str, BmsGroup] = {}

    for bms in bms_list:
        bms.set_keep_alive(user_config.get('keep_alive', False))

        if isinstance(bms, VirtualGroupBms):
            group_bms = bms
            for member_ref in bms.get_member_refs():
                if member_ref not in bms_by_name:
                    raise Exception("unknown bms %s in group %s" % (member_ref, group_bms))
                member_name = bms_by_name[member_ref].name
                if member_name in groups_by_bms:
                    raise Exception("can't add bms %s to multiple groups %s %s", member_name,
                                    groups_by_bms[member_name], group_bms)
                groups_by_bms[member_name] = group_bms.group
                bms.add_member(bms_by_name[member_ref])

    port_idx = user_config.mqtt_broker.rfind(':')
    if port_idx > 0:
        user_config.mqtt_port = user_config.get('mqtt_port', int(user_config.mqtt_broker[(port_idx + 1):]))
        user_config.mqtt_broker = user_config.mqtt_broker[:port_idx]

    logger.info('connecting mqtt %s@%s', user_config.mqtt_user, user_config.mqtt_broker)
    # paho_monkey_patch()
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

    if not user_config.mqtt_broker:
        mqtt_util.disable_warnings()

    from bmslib.store import load_meter_states
    try:
        meter_states = load_meter_states()
    except FileNotFoundError:
        logger.info("Initialize meter states file")
        meter_states = {}
    except Exception as e:
        logger.warning('Failed to load meter states: %s', e)
        meter_states = {}

    sample_period = float(user_config.get('sample_period', 1.0))
    publish_period = float(user_config.get('publish_period', sample_period))
    expire_values_after = float(user_config.get('expire_values_after', MIN_VALUE_EXPIRY))
    ic = user_config.get('invert_current', False)
    sampler_list = [BmsSampler(
        bms, mqtt_client=mqtt_client,
        dt_max_seconds=max(4., sample_period * 2),
        expire_after_seconds=max(expire_values_after, int(sample_period * 2 + .5), int(publish_period * 2 + .5)),
        invert_current=ic,
        meter_state=meter_states.get(bms.name),
        publish_period=publish_period,
        algorithms=dev_args[bms.name].get('algorithm') and dev_args[bms.name].get('algorithm', '').split(";"),
        current_calibration_factor=dev_args[bms.name].get('current_calibration', 1.0),
        bms_group=groups_by_bms.get(bms.name),
    ) for bms in bms_list]

    # move groups to the end
    sampler_list = sorted(sampler_list, key=lambda s: isinstance(s.bms, VirtualGroupBms))

    parallel_fetch = user_config.get('concurrent_sampling', False)

    logger.info('Fetching %d BMS + %d others %s, period=%.2fs, keep_alive=%s', len(sampler_list), len(extra_tasks),
                'concurrently' if parallel_fetch else 'serially', sample_period, user_config.get('keep_alive', False))

    watchdog_en = user_config.get('watchdog', False)
    max_errors = 200 if watchdog_en else 0

    asyncio.create_task(background_loop(
        timeout=max(15 * 60., sample_period * 4) if watchdog_en else 0,
        sampler_list=sampler_list
    ))

    tasks = sampler_list + extra_tasks

    # before we start the loops connect to each bms
    for t in tasks:
        try:
            await t()
        except:
            pass

    if parallel_fetch:
        # parallel_fetch now uses a loop for each BMS so they don't delay each other

        loops = [asyncio.create_task(fetch_loop(fn, period=sample_period, max_errors=max_errors)) for fn in tasks]
        await asyncio.wait(loops, return_when='FIRST_COMPLETED')
        for task in loops:
            task.done() or task.cancel()

    else:
        async def fn():
            if parallel_fetch:
                # concurrent synchronised fetch
                # this branch is currently not reachable!
                await asyncio.gather(*[t() for t in tasks], return_exceptions=False)
            else:
                random.shuffle(tasks)
                exceptions = []
                for t in tasks:
                    try:
                        await t()
                    except Exception as ex:
                        exceptions.append(ex)
                if exceptions:
                    logger.error('%d exceptions occurred fetching BMSs', len(exceptions))
                    raise exceptions[0]

        await fetch_loop(fn, period=sample_period, max_errors=max_errors)

    global shutdown
    logger.info('All fetch loops ended. shutdown is already %s', shutdown)
    shutdown = True

    store_states(sampler_list)

    for bms in bms_list:
        try:
            logger.info("Disconnecting %s", bms)
            await bms.disconnect()
            # await asyncio.sleep(2)
        except:
            pass


def on_exit(*args, **kwargs):
    global shutdown
    logger.info('exit signal handler... %s, %s, shutdown already %s', args, kwargs, shutdown)
    shutdown = True


atexit.register(on_exit)
# noinspection PyTypeChecker
signal.signal(signal.SIGTERM, on_exit)
# noinspection PyTypeChecker
signal.signal(signal.SIGINT, on_exit)

try:
    asyncio.run(main())
except Exception as e:
    logger.error("Main loop exception: %s", e)
    logger.error("Stack: %s", traceback.format_exc())

sys.exit(1)
