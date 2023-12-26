import asyncio
import math
import random
import re
import sys
import time
from collections import defaultdict
from copy import copy
from typing import Optional, List, Dict

import paho.mqtt.client

import bmslib.bt
from bmslib.algorithm import create_algorithm, BatterySwitches
from bmslib.bms import DeviceInfo, BmsSample, MIN_VALUE_EXPIRY
from bmslib.cache.mem import mem_cache_deco
from bmslib.group import BmsGroup, GroupNotReady
from bmslib.pwmath import Integrator, DiffAbsSum, LHQ
from bmslib.util import get_logger
from mqtt_util import publish_sample, publish_cell_voltages, publish_temperatures, publish_hass_discovery, \
    subscribe_switches, mqtt_single_out, round_to_n

logger = get_logger(verbose=False)


class SampleExpiredError(Exception):
    pass


class PeriodicBoolSignal:
    def __init__(self, period):
        self.period = period
        self._last_t = 0
        self.state = True

    def __bool__(self):
        return self.state

    def get(self):
        return self.state

    def set_time(self, t):
        if self._last_t == 0:
            self._last_t = t

        dt = t - self._last_t

        if dt < self.period:
            if self.state:
                self.state = False
        else:
            self._last_t = t
            self.state = True


class BmsSampleSink:
    """ Interface of an arbitrary data sink of battery samples """

    def publish_sample(self, bms_name: str, sample: BmsSample):
        raise NotImplementedError()

    def publish_voltages(self, bms_name: str, voltages: List[int]):
        raise NotImplementedError()

    def publish_meters(self, bms_name: str, readings: Dict[str, float]):
        raise NotImplementedError()


class BmsSampler:
    """
    Samples a single BMS and schedules publishing the samples to MQTT and arbitrary sinks.
    Also updates meters.
    """

    def __init__(self, bms: bmslib.bt.BtBms,
                 mqtt_client: paho.mqtt.client.Client,
                 dt_max_seconds,
                 expire_after_seconds,
                 invert_current=False,
                 meter_state=None,
                 publish_period=None,
                 sinks: Optional[List[BmsSampleSink]] = None,
                 algorithms: Optional[list] = None,
                 current_calibration_factor=1.0,
                 over_power=None,
                 bms_group: Optional[BmsGroup] = None
                 ):

        self.bms = bms
        self.mqtt_topic_prefix = re.sub(r'[^\w_.-/]', '_', bms.name)
        self.mqtt_client = mqtt_client
        self.invert_current = invert_current
        self.expire_after_seconds = expire_after_seconds
        self.device_info: Optional[DeviceInfo] = None
        self.num_samples = 0
        self.bms_group = bms_group  # group, virtual, parent
        self.current_calibration_factor = current_calibration_factor
        self.over_power = over_power or math.nan

        self.sinks = sinks or []

        self.downsampler = Downsampler()

        self.period_pub = PeriodicBoolSignal(period=publish_period or 0)
        self.period_discov = PeriodicBoolSignal(60 * 5)
        self.period_30s = PeriodicBoolSignal(period=30)

        self._t_wd_reset = time.time()  # watchdog
        self._last_time_log = 0

        self._last_power = 0
        self._t_last_power_jump = 0

        self._num_errors = 0
        self._time_next_retry = 0

        self.algorithm = None
        if algorithms:
            assert len(algorithms) == 1, "currently only 1 algo supported"
            algorithm = algorithms[0]
            self.algorithm = create_algorithm(algorithm, bms_name=bms.name)

        dx_max = dt_max_seconds / 3600
        self.current_integrator = Integrator(name="total_charge", dx_max=dx_max)
        self.power_integrator = Integrator(name="total_energy", dx_max=dx_max)
        self.power_integrator_discharge = Integrator(name="total_energy_discharge", dx_max=dx_max)
        self.power_integrator_charge = Integrator(name="total_energy_charge", dx_max=dx_max)

        dx_max_diff = 3600 / 3600  # allow larger gabs for already integrated value
        self.cycle_integrator = DiffAbsSum(name="total_cycles", dx_max=dx_max_diff, dy_max=0.1)
        self.charge_integrator = DiffAbsSum(name="total_abs_diff_charge", dx_max=dx_max_diff, dy_max=0.5)
        # TODO normalize dy_max to capacity                                                         ^^^

        self.meters = [self.current_integrator, self.power_integrator, self.power_integrator_discharge,
                       self.power_integrator_charge, self.cycle_integrator, self.charge_integrator]

        for meter in self.meters:
            if meter_state and meter.name in meter_state:
                meter.restore(meter_state[meter.name]['reading'])

        # self.power_stats = EWM(span=120, std_regularisation=0.1)

        temp_step = getattr(bms, 'TEMPERATURE_STEP', 0)
        temp_smooth = getattr(bms, 'TEMPERATURE_SMOOTH', 10)
        self._lhq_temp = defaultdict(lambda: LHQ(span=temp_smooth, inp_q=temp_step)) if temp_step else None

    def get_meter_state(self):
        return {meter.name: dict(reading=meter.get()) for meter in self.meters}

    async def __call__(self):
        self._num_errors += 1
        t_now = time.time()

        try:
            s = await self._sample_inner()
            if s:
                self._num_errors = 0
            return s
        except bmslib.bt.BleakDeviceNotFoundError:
            t_wait = min(1.5 ** self._num_errors, 120)
            logger.error("%s device not found, retry in %d seconds", self.bms, t_wait)
            self._time_next_retry = time.time() + t_wait
            return None

        except SampleExpiredError as e:
            logger.warning("%s: expired: %s", self.bms.name, e)
            return None

        except GroupNotReady as e:
            log_data = (t_now - self._last_time_log) >= (60 if self.num_samples < 1000 else 300) or self.bms.verbose_log
            if log_data:
                self._last_time_log = t_now
                logger.warning("%s: Group not ready: %s", self.bms.name, e)
            return None

        except Exception as ex:
            logger.error('%s error (#%d): %s', self.bms.name, self._num_errors, str(ex) or str(type(ex)), exc_info=1)
            dd = self.bms.debug_data()
            dd and logger.info("%s bms debug data: %s", self.bms.name, dd)
            self.device_info and logger.info('%s device info: %s', self.bms.name, self.device_info)
            logger.info('Bleak version %s', bmslib.bt.bleak_version())

            bms = self.bms
            t_interact = max(self._t_wd_reset, self.bms.connect_time)
            if bms.is_connected and time.time() - t_interact > 2 * max(MIN_VALUE_EXPIRY, self.expire_after_seconds):
                logger.warning('%s disconnect because no data has been flowing for some time', bms.name)
                await bms.disconnect()

            if bms.is_connected and self._num_errors > 20:
                logger.warning("disconnecting %s due to too many errors %d", bms, self._num_errors)
                await bms.disconnect()
                self._num_errors = 0

            raise

    @mem_cache_deco(ttl=30)
    async def _fetch_temperatures_cached(self):
        try:
            return await self.bms.fetch_temperatures()
        except:
            return None

    def _filter_temperatures(self, temperatures):
        if not temperatures or self._lhq_temp is None:
            return temperatures
        return [round(self._lhq_temp[i].add(temperatures[i]), 2) for i in range(len(temperatures))]

    async def _sample_inner(self):
        bms = self.bms
        mqtt_client = self.mqtt_client

        was_connected = bms.is_connected

        # if not was_connected:
        #    self._num_errors = 0

        t_conn = time.time()

        if not was_connected and t_conn < self._time_next_retry:
            logger.debug('retry in %.0f sec', self._time_next_retry - t_conn)
            await asyncio.sleep(4)
            return None

        if not was_connected and not bms.is_virtual:
            logger.info('connecting bms %s', bms)

        async with bms:
            if not was_connected:
                logger.info('connected bms %s!', bms)

            if self.device_info is None and self.num_samples == 0:
                # try to fetch device info first. if bms.fetch() fails we might have at least some details
                await self._try_fetch_device_info()

            t_fetch = time.time()

            sample = await bms.fetch()

            t_now = time.time()
            t_hour = t_now * (1 / 3600)

            if sample.timestamp < t_now - max(self.expire_after_seconds, MIN_VALUE_EXPIRY):
                raise SampleExpiredError("sample %s expired" % sample.timestamp)
                # logger.warning('%s expired sample', bms.name)
                # return

            sample.num_samples = self.num_samples

            if self.current_calibration_factor and self.current_calibration_factor != 1:
                sample = sample.multiply_current(self.current_calibration_factor)

            # discharging P>0
            self.power_integrator_charge += (t_hour, abs(min(0, sample.power)) * 1e-3)  # kWh
            self.power_integrator_discharge += (t_hour, abs(max(0, sample.power)) * 1e-3)  # kWh

            # self.power_stats.add(sample.power)

            if (self.sinks or self.bms_group) and not sample.temperatures:
                sample.temperatures = await self._fetch_temperatures_cached()

            sample.temperatures = self._filter_temperatures(sample.temperatures)

            if not math.isnan(sample.mos_temperature) and self._lhq_temp is not None:
                sample.mos_temperature = self._lhq_temp['mos'].add(sample.mos_temperature)

            if self.bms_group:
                # update before invert current
                self.bms_group.update(bms, sample)

            if self.invert_current:
                sample = sample.invert_current()

            self.current_integrator += (t_hour, sample.current)  # Ah
            self.power_integrator += (t_hour, sample.power * 1e-3)  # kWh

            self.cycle_integrator += (t_hour, sample.soc * (0.01 / 2))  # SoC 100->0 is a half cycle
            self.charge_integrator += (t_hour, sample.charge)  # Ah

            if self.algorithm:
                res = self.algorithm.update(sample)
                if res or self.bms.verbose_log:
                    logger.info('Algo State=%s (bms=%s) -> %s ', self.algorithm.state,
                                BatterySwitches(**sample.switches), res)

                if res:
                    from bmslib.store import store_algorithm_state
                    state = self.algorithm.state
                    if state:
                        store_algorithm_state(bms.name, algorithm_name=self.algorithm.name, state=state.__dict__)

                if res and res.switches:
                    for swk in sample.switches.keys():
                        if res.switches[swk] is not None:
                            logger.info('%s algo set %s switch -> %s', bms.name, swk, res.switches[swk])
                            await self.bms.set_switch('charge', res.switches[swk])

            if self.num_samples == 0 and sample.switches and mqtt_client:
                logger.info("%s subscribing for %s switch change", bms.name, sample.switches)
                subscribe_switches(mqtt_client, device_topic=self.mqtt_topic_prefix, bms=bms,
                                   switches=sample.switches.keys())

            for sink in self.sinks:
                try:
                    sink.publish_sample(bms.name, sample)
                except:
                    logger.error(sys.exc_info(), exc_info=True)

            self.downsampler += sample

            log_data = (t_now - self._last_time_log) >= (60 if self.num_samples < 1000 else 300) or bms.verbose_log
            if log_data:
                self._last_time_log = t_now

            voltages = []

            async def cached_fetch_voltages():
                nonlocal voltages
                if voltages:
                    return voltages

                # TODO fetch_voltages at t_fetch interval and down-sampling?
                try:
                    voltages = await bms.fetch_voltages()

                    if self.bms_group:
                        self.bms_group.update_voltages(bms, voltages)
                except:
                    logger.error("%s error fetching voltage", bms.name, exc_info=1)
                    voltages = None

                return voltages

            if self.sinks:
                voltages = await cached_fetch_voltages()
                for sink in self.sinks:
                    sink.publish_voltages(bms.name, voltages)

            # z_score = self.power_stats.z_score(sample.power)
            # if abs(z_score) > 12:
            #    logger.info('%s Power z_score %.1f (avg=%.0f std=%.2f last=%.0f)', bms.name, z_score, self.power_stats.avg.value, self.power_stats.stddev, sample.power)

            PWR_CHG_REG = 120  # regularisation to suppress changes when power is low
            PWR_CHG_HOLD = 4
            power_chg = (sample.power - self._last_power) / (abs(self._last_power) + PWR_CHG_REG)
            if not bms.is_virtual and abs(power_chg) > 0.15 and abs(sample.power) > abs(self._last_power):
                if bms.verbose_log or (
                        not self.period_pub and (t_now - self._t_last_power_jump) > PWR_CHG_HOLD):
                    logger.info('%s Power jump %.0f %% (prev=%.0f last=%.0f, REG=%.0f)', bms.name, power_chg * 100,
                                self._last_power, sample.power, PWR_CHG_REG)
                self._t_last_power_jump = t_now
            self._last_power = sample.power

            if self.period_discov or self.period_pub or \
                    (t_now - self._t_last_power_jump) < PWR_CHG_HOLD or abs(sample.power) > self.over_power:
                self._t_pub = t_now

                sample = self.downsampler.pop()

                publish_sample(mqtt_client, device_topic=self.mqtt_topic_prefix, sample=sample)
                log_data and logger.info('%s: %s', bms.name, sample)

                voltages = await cached_fetch_voltages()
                publish_cell_voltages(mqtt_client, device_topic=self.mqtt_topic_prefix, voltages=voltages)

                # temperatures = None
                if self.period_30s or self.period_discov:
                    if not sample.temperatures:
                        sample.temperatures = await self._fetch_temperatures_cached()
                        sample.temperatures = self._filter_temperatures(sample.temperatures)
                    publish_temperatures(mqtt_client, device_topic=self.mqtt_topic_prefix,
                                         temperatures=sample.temperatures)

                if log_data and (voltages or sample.temperatures) and not bms.is_virtual:
                    logger.info('%s volt=[%s] temp=%s', bms.name, ','.join(map(str, voltages)),
                                sample.temperatures)

            if self.period_discov or self.period_30s:
                self.publish_meters()

            # publish home assistant discovery every 60 samples
            if self.period_discov:
                logger.info("Sending HA discovery for %s (num_samples=%d)", bms.name, self.num_samples)
                if self.device_info is None:
                    await self._try_fetch_device_info()
                publish_hass_discovery(
                    mqtt_client, device_topic=self.mqtt_topic_prefix,
                    expire_after_seconds=self.expire_after_seconds,
                    sample=sample,
                    num_cells=len(voltages) if voltages else 0,
                    temperatures=sample.temperatures,
                    device_info=self.device_info,
                )

                # publish sample again after discovery
                if self.period_pub.period > 2:
                    await asyncio.sleep(1)
                    publish_sample(mqtt_client, device_topic=self.mqtt_topic_prefix, sample=sample)

        self.num_samples += 1
        t_disc = time.time()
        self._t_wd_reset = sample.timestamp or t_disc

        self.period_pub.set_time(t_now)
        self.period_30s.set_time(t_now)
        self.period_discov.set_time(t_now)

        dt_conn = t_fetch - t_conn
        dt_fetch = t_disc - t_fetch
        dt_max = max(dt_conn, dt_fetch)
        if bms.verbose_log or (  # or dt_max > 1
                dt_max > 0.01 and random.random() < (0.05 if sample.num_samples < 1e3 else 0.01)
                and not bms.is_virtual and log_data):
            logger.info('%s times: connect=%.2fs fetch=%.2fs', bms, dt_conn, dt_fetch)

        return sample

    def publish_meters(self):
        device_topic = self.mqtt_topic_prefix
        for meter in self.meters:
            topic = f"{device_topic}/meter/{meter.name}"
            s = round_to_n(meter.get(), 4)
            mqtt_single_out(self.mqtt_client, topic, s)

        if self.sinks:
            readings = {m.name: m.get() for m in self.meters}
            for sink in self.sinks:
                try:
                    sink.publish_meters(self.bms.name, readings)
                except NotImplementedError:
                    pass
                except:
                    logger.error(sys.exc_info(), exc_info=True)

    async def _try_fetch_device_info(self):
        try:
            self.device_info = await self.bms.fetch_device_info()
        except NotImplementedError:
            pass
        except Exception as e:
            logger.warning('%s error fetching device info: %s', self.bms.name, e)


class Downsampler:
    """ Averages multiple BmsSamples """

    def __init__(self):
        self._power = 0
        self._current = 0
        self._voltage = 0
        self._num = 0
        self._last: Optional[BmsSample] = None

    def __iadd__(self, s: BmsSample):
        self._power += s._power
        self._current += s.current
        self._voltage += s.voltage
        self._num += 1
        self._last = s
        return self

    def pop(self):
        if self._num == 0:
            return None

        if self._num == 1:
            return self._last

        n = 1 / self._num
        s = copy(self._last)

        if not math.isnan(s._power):
            s._power = self._power * n
        s.current = self._current * n
        s.voltage = self._voltage * n

        self._power = 0
        self._current = 0
        self._voltage = 0
        self._num = 0
        self._last = None

        return s
