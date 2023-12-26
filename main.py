import asyncio
import atexit
import os
import random
import re
import signal
import sys
import threading
import time
import traceback
from typing import List, Dict, Tuple

import paho.mqtt.client as paho

import bmslib.bt
import bmslib.models.ant
import bmslib.models.daly
import bmslib.models.dummy
import bmslib.models.jbd
import bmslib.models.jikong
import bmslib.models.sok
import bmslib.models.supervolt
import bmslib.models.victron
import mqtt_util
from bmslib.bms import MIN_VALUE_EXPIRY
from bmslib.group import VirtualGroupBms, BmsGroup
from bmslib.sampling import BmsSampler
from bmslib.store import load_user_config
from bmslib.util import get_logger, exit_process
from mqtt_util import mqtt_last_publish_time, mqtt_message_handler, mqtt_process_action_queue

logger = get_logger(verbose=False)

user_config: Dict[str, any] = load_user_config()

shutdown = False
t_last_store = 0


async def fetch_loop(fn, period, max_errors):
    num_errors_row = 0
    while not shutdown:
        try:
            if await fn():
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


def bg_checks(sampler_list, timeout, t_start):
    global shutdown

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
            return False

    global t_last_store
    # store persistent states (metering) every 30s
    if now - (t_last_store or t_start) > 30:
        t_last_store = now
        try:
            store_states(sampler_list)
        except Exception as e:
            logger.error('Error storing states: %s', e)

    return True


def background_thread(timeout: float, sampler_list: List[BmsSampler]):
    t_start = time.time()
    while not shutdown:
        if not bg_checks(sampler_list, timeout, t_start):
            break
        time.sleep(4)
    logger.info("Background thread ends. shutdown=%s", shutdown)
    time.sleep(10)
    logger.info("Process still alive, suicide")
    exit_process(True, True)


async def background_loop(timeout: float, sampler_list: List[BmsSampler]):
    global shutdown

    t_start = time.time()

    if timeout:
        logger.info("mqtt watchdog loop started with timeout %.1fs", timeout)

    while not shutdown:

        await mqtt_process_action_queue()
        if not bg_checks(sampler_list, timeout, t_start):
            break

        await asyncio.sleep(.1)


async def main():
    global shutdown

    bms_list: List[bmslib.bt.BtBms] = []
    extra_tasks = []  # currently unused, add custom coroutines here. must return True on success and can raise

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
        devices = await asyncio.wait_for(bmslib.bt.bt_discovery(logger), 30)
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
        sok=bmslib.models.sok.SokBt,
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
        **{bms.address: bms for bms in bms_list if not bms.is_virtual},
        **{bms.name: bms for bms in bms_list}}
    groups_by_bms: Dict[str, BmsGroup] = {}

    for bms in bms_list:
        bms.set_keep_alive(user_config.get('keep_alive', False))

        if bms.is_virtual:
            group_bms = bms
            for member_ref in bms.get_member_refs():
                if member_ref not in bms_by_name:
                    logger.warning('BMS names: %s', set(bms_by_name.keys()))
                    logger.warning('Please choose one of these names')
                    raise Exception("unknown bms '%s' in group %s" % (member_ref, group_bms))
                member_name = bms_by_name[member_ref].name
                if member_name in groups_by_bms:
                    raise Exception("can't add bms %s to multiple groups %s %s", member_name,
                                    groups_by_bms[member_name], group_bms)
                groups_by_bms[member_name] = group_bms.group
                bms.add_member(bms_by_name[member_ref])

    # import env vars from addon_main.sh
    for k, en in dict(mqtt_broker='MQTT_HOST', mqtt_user='MQTT_USER', mqtt_password='MQTT_PASSWORD').items():
        if not user_config.get(k) and os.environ.get(en):
            user_config[k] = os.environ[en]

    if user_config.get('mqtt_broker'):
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
    else:
        mqtt_client = None

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

    sinks = []
    if user_config.get('influxdb_host', None):
        from bmslib.sinks import InfluxDBSink
        sinks.append(InfluxDBSink(**{k[9:]: v for k, v in user_config.items() if k.startswith('influxdb_')}))

    if user_config.get("telemetry"):
        try:
            from bmslib.sinks import TelemetrySink
            sinks.append(TelemetrySink(bms_by_name=bms_by_name))
        except:
            logger.warning("failed to init telemetry", exc_info=True)



    sampler_list = [BmsSampler(
        bms, mqtt_client=mqtt_client,
        dt_max_seconds=max(60. * 10, sample_period * 2),
        expire_after_seconds=expire_values_after and max(expire_values_after, int(sample_period * 2 + .5),
                                                         int(publish_period * 2 + .5)),
        invert_current=ic,
        meter_state=meter_states.get(bms.name),
        publish_period=publish_period,
        algorithms=dev_args[bms.name].get('algorithm') and dev_args[bms.name].get('algorithm', '').split(";"),
        current_calibration_factor=float(dev_args[bms.name].get('current_calibration', 1.0)),
        bms_group=groups_by_bms.get(bms.name),
        sinks=sinks,
    ) for bms in bms_list]

    # move groups to the end
    sampler_list = sorted(sampler_list, key=lambda s: bms.is_virtual)

    parallel_fetch = user_config.get('concurrent_sampling', False)

    logger.info('Fetching %d BMS + %d virtual + %d others %s, period=%.2fs, keep_alive=%s',
                sum(not bms.is_virtual for bms in bms_list),
                sum(bms.is_virtual for bms in bms_list), len(extra_tasks),
                'concurrently' if parallel_fetch else 'serially', sample_period, user_config.get('keep_alive', False))

    watchdog_en = user_config.get('watchdog', False)
    max_errors = 200 if watchdog_en else 0

    wd_timeout = max(5 * 60., sample_period * 4) if watchdog_en else 0
    asyncio.create_task(background_loop(
        timeout=wd_timeout,
        sampler_list=sampler_list
    ))

    # add another daemon thread, asyncio can dead-lock with bleak TODO bug?
    threading.Thread(target=lambda: background_thread(wd_timeout, sampler_list), daemon=True).start()

    tasks = sampler_list + extra_tasks

    # before we start the loops connect to each bms in random order
    tasks_shuffle = list(tasks)
    random.shuffle(tasks_shuffle)
    for t in tasks_shuffle:
        if isinstance(t, BmsSampler) and t.bms.is_virtual:
            continue
        try:
            await t()
        except:
            pass

    if parallel_fetch:
        # parallel_fetch now uses a loop for each BMS, so they don't delay each other

        # this outer while loop recovers from a cancelled task. this happens when a device disconnects (bleak bug?)
        while not shutdown:
            loops = [asyncio.create_task(fetch_loop(fn, period=sample_period, max_errors=max_errors)) for fn in tasks]
            done, pending = await asyncio.wait(loops, return_when='FIRST_COMPLETED')

            logger.debug('Done= %s, Pending=%s', done, pending)
            for task in loops:
                logger.debug('Task %s is done=%s', task, task.done())
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

    logger.info('All fetch loops ended. shutdown is already %s', shutdown)
    shutdown = True

    store_states(sampler_list)

    for sink in sinks:
        try:
            sink.flush()
        except:
            pass

    for bms in bms_list:
        try:
            logger.info("Disconnecting %s", bms)
            await bms.disconnect()
            # await asyncio.sleep(2)
        except:
            pass


def on_exit(*args, **kwargs):
    global shutdown
    logger.info('exit signal handler... %s, %s, shutdown was %s', args, kwargs, shutdown)
    shutdown += 1
    bmslib.bt.BtBms.shutdown = True
    if shutdown == 5:
        sys.exit(1)


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
