import asyncio
import contextlib
import math
import random
import re
import sys
import time
from collections import defaultdict
from copy import copy
from typing import Optional, List, Dict

import bleak.exc
import paho.mqtt.client

import bmslib.bt
from bmslib.algorithm import create_algorithm
from bmslib.bms import DeviceInfo, BmsSample, MIN_VALUE_EXPIRY
from bmslib.cache.mem import mem_cache_deco
from bmslib.group import BmsGroup, GroupNotReady
from bmslib.mqtt_util import publish_sample, publish_cell_voltages, publish_temperatures, publish_hass_discovery, \
    subscribe_switches, mqtt_single_out
from bmslib.pwmath import Integrator, DiffAbsSum, LHQ
from bmslib.util import get_logger, summarize_exc

logger = get_logger(verbose=False)


class SampleExpiredError(Exception):
    pass


# bt_power_cycle_on_error (#392): all samplers share the host controller, so the
# cycle is rate-limited process-wide - three wedged BMS must not cycle the radio
# three times in a row. It also drops every *healthy* connection, which is why it
# is opt-in and only reached after repeated reconnects of one BMS have failed.
BT_POWER_CYCLE_MIN_INTERVAL = 600
BT_POWER_CYCLE_SETTLE = 3  # seconds the controller stays down / gets to come back up
_t_last_bt_power_cycle = 0.0


async def bt_power_cycle(bms_name):
    """Power the bluetooth controller(s) off and on, last-resort recovery for a
    host stack that no longer completes connects (BlueZ answering every attempt
    with 'Operation already in progress', #370).

    Returns True if the cycle was *attempted*, False if it was rate-limited or
    raised. It cannot promise the controller actually came back: bt_power() logs
    and swallows bluetoothctl failures internally, so success here is not
    observable from the return value.

    Goes through bt_power() rather than hciconfig so the ble_stack guards apply:
    with bumble the adapter is owned by bumble itself, and with esphome there is
    no local adapter to cycle. bt_power shells out to bluetoothctl, so it runs in
    a thread - other samplers share this loop.
    """
    global _t_last_bt_power_cycle
    t_now = time.time()
    dt = t_now - _t_last_bt_power_cycle
    if _t_last_bt_power_cycle and dt < BT_POWER_CYCLE_MIN_INTERVAL:
        logger.info('%s: skipping bt power cycle, last one was %.0fs ago (min %ds)',
                    bms_name, dt, BT_POWER_CYCLE_MIN_INTERVAL)
        return False

    # claim the slot before the first await, so two samplers on one loop cannot
    # interleave their way into two cycles
    _t_last_bt_power_cycle = t_now
    logger.warning('%s: still failing after repeated reconnects, power-cycling the '
                   'bluetooth controller(s). This drops all BLE connections.', bms_name)
    powered_off = False
    try:
        await asyncio.to_thread(bmslib.bt.bt_power, False)
        powered_off = True
        await asyncio.sleep(BT_POWER_CYCLE_SETTLE)  # settle before powering back up
        await asyncio.to_thread(bmslib.bt.bt_power, True)
        await asyncio.sleep(BT_POWER_CYCLE_SETTLE)
    except asyncio.CancelledError:
        # main.py cancels the pending fetch loops whenever one of them returns.
        # Being cancelled between off and on would leave the radio down for good,
        # so hand the power-on to a task that outlives this coroutine.
        if powered_off:
            logger.warning('bt power cycle cancelled while the controller was down, '
                           'powering back up')
            asyncio.ensure_future(asyncio.to_thread(bmslib.bt.bt_power, True))
        raise
    except Exception as e:
        logger.error('bt power cycle failed: %s', str(e) or type(e).__name__)
        if powered_off:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(bmslib.bt.bt_power, True)
        return False

    logger.info('bt power cycle attempted (bluetoothctl does not report success)')
    return True


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

    def publish_sample(self, bms_name: str, sample: BmsSample, tags=None):
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
                 bms_group: Optional[BmsGroup] = None,
                 bt_power_cycle_on_error=False,
                 reconnect_interval_s: Optional[float] = None,
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
        self._num_not_found = 0  # consecutive device-not-found, drives the retry backoff
        self._num_error_disconnects = 0  # consecutive error-forced reconnects, drives the power cycle
        self._time_next_retry = 0
        self._last_diag_t = 0
        self.bt_power_cycle_on_error = bt_power_cycle_on_error

        # Periodic reconnect (reconnect_interval_minutes, off by default). With
        # keep_alive a BMS stays on whichever backend/proxy it first connected
        # through: habluetooth only scores backends inside connect(). On a
        # multi-proxy esphome setup that first pick is often not the best one, so
        # optionally drop the link now and then to let connect() choose again.
        # Jittered +/-20% and re-rolled per connect, so a fleet that connected
        # in the same startup window does not reconnect in one burst every cycle.
        self._reconnect_interval_s = reconnect_interval_s if reconnect_interval_s and reconnect_interval_s > 0 else None
        self._t_connected = 0.0
        self._reconnect_due_s = math.inf

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

    def _arm_periodic_reconnect(self):
        self._t_connected = time.monotonic()  # immune to wall-clock steps, like estimate_runtime
        if self._reconnect_interval_s:
            self._reconnect_due_s = self._reconnect_interval_s * random.uniform(0.8, 1.2)

    async def _periodic_reconnect(self):
        """Drop a healthy keep_alive link once its jittered interval elapsed."""
        bms = self.bms
        if not self._reconnect_interval_s or bms.is_virtual or not bms.is_connected:
            return
        if bms.address == 'serial':
            return  # wired: nothing to re-pick
        age = time.monotonic() - self._t_connected
        if age < self._reconnect_due_s:
            return
        logger.info('%s periodic reconnect after %.0f s', bms.name, age)
        self._reconnect_due_s = math.inf  # once per connect, even if the teardown fails
        async with bmslib.bt.ConnectLock:
            await bms.force_disconnect()

    def get_meter_state(self):
        return {meter.name: dict(reading=meter.get()) for meter in self.meters}

    async def __call__(self):
        self._num_errors += 1
        t_now = time.time()

        try:
            s = await self._sample_inner()
            if s:
                self._num_errors = 0
                self._num_not_found = 0
                self._num_error_disconnects = 0
            return s
        except (bmslib.bt.BleakDeviceNotFoundError, bmslib.bt.BleakNotFoundError) as e:
            # back off on the number of failed *connects*, not on _num_errors: the latter
            # also counts the cycles spent waiting in _sample_inner(), so the wait fed
            # itself and hit the 291 s cap after 3 failures (#391).
            self._num_not_found += 1
            t_wait = 1.5 ** min(self._num_not_found + 4, 14)
            logger.error("%s device not found, retry in %d seconds (%s)", self.bms, t_wait, str(e) or type(e).__name__)
            self._time_next_retry = time.time() + t_wait
            return None

        except SampleExpiredError as e:
            if self._num_errors < 3:
                logger.warning("%s: expired: %s", self.bms.name, e)
            return None

        except GroupNotReady as e:
            log_data = (t_now - self._last_time_log) >= (60 if self.num_samples < 1000 else 300) or self.bms.verbose_log
            if log_data:
                self._last_time_log = t_now
                logger.warning("%s: Group not ready: %s", self.bms.name, e)
            return None

        except Exception as ex:
            # Collapse the multi-page asyncio.wait_for traceback that masks the
            # real cause for connect/notify timeouts (see #367, #324). Full
            # exc_info kept for unexpected types where the trace is informative.
            short_trace_types = (TimeoutError, asyncio.TimeoutError, OSError,
                                 bleak.exc.BleakError)
            if isinstance(ex, bmslib.bt.BleakCharacteristicNotFoundError):
                logger.error('%s error (#%d): %s', self.bms.name, self._num_errors,
                             str(ex) or type(ex).__name__)
            elif isinstance(ex, short_trace_types):
                logger.error('%s error (#%d): %s', self.bms.name, self._num_errors,
                             summarize_exc(ex))
            else:
                logger.error('%s error (#%d): %s', self.bms.name, self._num_errors,
                             str(ex) or str(type(ex)), exc_info=True)

            dd = self.bms.debug_data()
            dd and logger.info("%s bms debug data: %s", self.bms.name, dd)
            self.device_info and logger.info('%s device info: %s', self.bms.name, self.device_info)

            if isinstance(ex, short_trace_types) and (t_now - self._last_diag_t) > 30:
                self._last_diag_t = t_now
                logger.info('%s stack: Bleak %s, %s', self.bms.name,
                            bmslib.bt.bleak_version(),
                            bmslib.bt.bt_stack_version())
                if self.bms.address != 'serial':
                    try:
                        await bmslib.bt.bt_diagnostics(
                            self.bms.address, getattr(self.bms, '_adapter', None),
                            logger, timeout=3.0)
                    except Exception as de:
                        logger.warning('%s bt_diagnostics failed: %s', self.bms.name,
                                       str(de) or type(de).__name__)

            bms = self.bms
            t_interact = max(self._t_wd_reset, self.bms.connect_time)
            if bms.is_connected and time.time() - t_interact > 2 * max(MIN_VALUE_EXPIRY, self.expire_after_seconds):
                logger.warning('%s disconnect because no data has been flowing for some time', bms.name)
                await bms.disconnect()

            if self._num_errors > 20:
                # One saturation round: drop the link if there is one, and count it
                # either way. Keying the count on is_connected made the escalation
                # below unreachable in the case it exists for - a host that answers
                # every connect with 'Operation already in progress' never gets the
                # link up, so is_connected stays False forever (#392).
                if bms.is_connected:
                    logger.warning("disconnecting %s due to too many errors %d", bms, self._num_errors)
                    await bms.disconnect()
                self._num_errors = 0
                self._num_error_disconnects += 1

            # Two saturation rounds (~42 cycles) with no sample in between, on a
            # BLE error rather than a downstream one, so it is not this battery
            # having a bad minute. Optionally cycle the controller (#392). Absent
            # devices cannot get here - BleakDeviceNotFoundError returns through
            # its own backoff above - and any good sample resets the count, so a
            # BMS that recovers never escalates.
            if (self._num_error_disconnects >= 2 and self.bt_power_cycle_on_error
                    and isinstance(ex, short_trace_types)
                    and not bms.is_virtual and bms.address != 'serial'):
                if await bt_power_cycle(bms.name):
                    self._num_error_disconnects = 0  # keep the history if it was skipped

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

        await self._periodic_reconnect()

        was_connected = bms.is_connected

        # if not was_connected:
        #    self._num_errors = 0

        t_conn = time.time()

        err = False

        if not was_connected and t_conn < self._time_next_retry:
            logger.debug('retry in %.0f sec', self._time_next_retry - t_conn)
            await asyncio.sleep(4)
            return None

        if not was_connected and not bms.is_virtual:
            logger.debug('connecting bms %s', bms)

        async with bms:
            # a successful connect ends the not-found streak. don't wait for a good
            # sample: _sample_inner returns None for a healthy BMS whose fetch_voltages
            # fails (err below), which would carry the old streak forever (#391).
            self._num_not_found = 0

            if not was_connected:
                logger.info('connected bms %s!', bms)
            if math.isinf(self._reconnect_due_s):
                # (re)arm on any fresh link, including one __aenter__ repaired after a
                # connect that raised half-way (was_connected was True then, #391)
                self._arm_periodic_reconnect()

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

            # Estimated seconds-to-empty, derived here for every BMS from
            # remaining charge / smoothed discharge current (batmon sign:
            # current > 0). Computed after calibration/invert so it uses the
            # canonical current sign. A BMS that reports its own runtime (e.g.
            # via aiobmsble) keeps it. VirtualGroupBms is duck-typed and not yet
            # a BtBms subclass, so it has no estimator (keeps runtime=nan, as
            # before); it'll get one for free once it inherits BtBms.
            if math.isnan(sample.runtime) and hasattr(bms, 'estimate_runtime'):
                sample.runtime = bms.estimate_runtime(sample)

            self.current_integrator += (t_hour, sample.current)  # Ah
            self.power_integrator += (t_hour, sample.power * 1e-3)  # kWh

            self.cycle_integrator += (t_hour, sample.soc * (0.01 / 2))  # SoC 100->0 is a half cycle
            self.charge_integrator += (t_hour, sample.charge)  # Ah

            if self.algorithm:
                res = self.algorithm.update(sample)
                if res or self.bms.verbose_log:
                    # sample.switches may carry keys BatterySwitches doesn't take
                    # (JK reports balance/float_charge too), so log the dict as-is
                    # instead of splatting it into BatterySwitches(charge, discharge)
                    # — the latter raised TypeError and killed the sample (#234).
                    logger.info('Algo State=%s (bms=%s) -> %s ', self.algorithm.state,
                                sample.switches, res)

                if res:
                    from bmslib.store import store_algorithm_state
                    state = self.algorithm.state
                    if state:
                        store_algorithm_state(bms.name, algorithm_name=self.algorithm.name, state=state.__dict__)

                if res and res.switches:
                    # Apply only the switches the algorithm set. Iterate
                    # BatterySwitches' own fields, NOT sample.switches — the BMS
                    # may report switches res.switches has no key for (JK:
                    # balance/float_charge → KeyError), and set_switch must target
                    # the switch that changed, not always 'charge' (#234).
                    for swk in ('charge', 'discharge'):
                        val = res.switches[swk]
                        if val is not None:
                            logger.info('%s algo set %s switch -> %s', bms.name, swk, val)
                            await self.bms.set_switch(swk, val)

            if self.num_samples == 0 and sample.switches and mqtt_client:
                logger.info("%s subscribing for %s switch change", bms.name, sample.switches)
                subscribe_switches(mqtt_client, device_topic=self.mqtt_topic_prefix, bms=bms,
                                   switches=sample.switches.keys())

            for sink in self.sinks:
                try:
                    sink.publish_sample(bms.name, sample)
                except Exception as e:
                    logger.error('sink %s publish_sample failed: %s',
                                 type(sink).__name__, summarize_exc(e))

            self.downsampler += sample

            log_data = (t_now - self._last_time_log) >= (60 if self.num_samples < 1000 else 300) or bms.verbose_log
            if log_data:
                self._last_time_log = t_now

            voltages = []

            async def cached_fetch_voltages():
                nonlocal voltages, err
                if voltages:
                    return voltages

                # TODO fetch_voltages at t_fetch interval and down-sampling?
                try:
                    voltages = await bms.fetch_voltages()

                    if self.bms_group:
                        self.bms_group.update_voltages(bms, voltages)
                except:
                    logger.error("%s error fetching voltage", bms.name, exc_info=1)
                    err = True
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
            PWR_CHG_HOLD = 4  # time in seconds to keep high frequency sampling after a power jump. this helps capture power transients and noise wave form
            power_chg = (sample.power - self._last_power) / (abs(self._last_power) + PWR_CHG_REG)
            if not bms.is_virtual and abs(power_chg) > 0.15 and abs(sample.power) > abs(self._last_power):
                if bms.verbose_log or (
                        not self.period_pub and (t_now - self._t_last_power_jump) > PWR_CHG_HOLD * 10):
                    logger.info('%s Power jump/noise %.0f %% (prev=%.0f last=%.0f, REG=%.0f)', bms.name,
                                power_chg * 100,
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

                # Publish temperatures every cycle so the HA entity doesn't
                # flicker to "unavailable" (#207). Temps change slowly, so
                # gating them to a 30s tick meant nothing republished them
                # between ticks while expire_after defaults to 20s. Publishing
                # every cycle lets mqtt_single_out's keep-alive republish
                # unchanged values every MIN_VALUE_EXPIRY/2 s, well within
                # expire_after. The separate BMS fetch stays rate-limited by its
                # 30s mem-cache, so this adds no extra BLE traffic.
                if not sample.temperatures:
                    sample.temperatures = await self._fetch_temperatures_cached()
                    sample.temperatures = self._filter_temperatures(sample.temperatures)
                publish_temperatures(mqtt_client, device_topic=self.mqtt_topic_prefix,
                                     temperatures=sample.temperatures)

                if log_data and (voltages or sample.temperatures) and not bms.is_virtual:
                    logger.info('%s volt=[%s] temp=%s', bms.name,
                                ','.join(map(str, voltages)) if voltages else voltages,
                                sample.temperatures)

            if self.period_discov or self.period_30s:
                self.publish_meters()

            # publish home assistant discovery every 60 samples
            if self.period_discov:
                logger.debug("Sending HA discovery for %s (num_samples=%d)", bms.name, self.num_samples)
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
                dt_max > 0.01 and random.random() < (0.05 if sample.num_samples < 1e3 else 0.01) * (dt_conn + dt_fetch)
                and not bms.is_virtual and log_data):
            if (dt_conn > 1e-2 or dt_fetch > 1e-2):
                logger.info('%s times: connect=%.2fs fetch=%.2fs', bms, dt_conn, dt_fetch)

        # pass "light" errors to the caller to trigger a re-connect after too many
        return sample if not err else None

    def publish_meters(self):
        device_topic = self.mqtt_topic_prefix
        for meter in self.meters:
            topic = f"{device_topic}/meter/{meter.name}"
            s = round(meter.get(), 3)
            mqtt_single_out(self.mqtt_client, topic, s)

        if self.sinks:
            readings = {m.name: m.get() for m in self.meters}
            for sink in self.sinks:
                try:
                    sink.publish_meters(self.bms.name, readings)
                except NotImplementedError:
                    pass
                except Exception as e:
                    logger.error('sink %s publish_meters failed: %s',
                                 type(sink).__name__, summarize_exc(e))

    async def _try_fetch_device_info(self):
        try:
            di = await self.bms.fetch_device_info()
            if self.device_info is None:
                logger.info('%s device_info=%s', self.bms.name, di)
            self.device_info = di
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
        self._power += s.power
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


async def fetch_loop(fn, period, max_errors, should_stop=None, max_backoff=60):
    """Drive `fn` every `period` seconds, aborting after `max_errors` consecutive failures.

    `num_errors_row` counts *consecutive* failing cycles: it drives the 1.1**n
    error backoff and the max_errors abort, and both only make sense that way.
    It used to be reset by `if await fn(): num_errors_row = 0`, but the serial
    fetch fn in main() returns None on success, so the reset never ran and the
    count grew for the lifetime of the add-on: every error slept the full 60 s
    cap after ~44 lifetime errors, and the watchdog aborted a perfectly healthy
    add-on once it reached 200 (#391).

    `max_backoff` caps the error sleep. 60 s suits the serial loop, where one
    device's failure would stall every other device behind it. A per-device
    loop (concurrent_sampling) can back off much further for a device that is
    never coming back, so its retries stop starving healthy neighbours sharing
    the same proxy (#405).
    """
    num_errors_row = 0
    while not (should_stop and should_stop()):
        try:
            await fn()
            num_errors_row = 0  # a cycle that did not raise is not an error
        except Exception as e:
            num_errors_row += 1
            # The per-sampler logger in BmsSampler.__call__ already logged a
            # collapsed one-line trace via summarize_exc; just record the
            # rolling count here without duplicating the multi-page traceback
            # (see #367). Keep full trace for unexpected non-BLE exception types.
            short_types = (TimeoutError, asyncio.TimeoutError, OSError,
                           bleak.exc.BleakError,
                           bmslib.bt.BleakCharacteristicNotFoundError)
            if isinstance(e, short_types):
                logger.error('Error (num %d, max %d) reading BMS: %s',
                             num_errors_row, max_errors, summarize_exc(e))
            else:
                import traceback
                logger.error('Error (num %d, max %d) reading BMS: %s',
                             num_errors_row, max_errors, e)
                logger.error('Stack: %s', traceback.format_exc())
            if max_errors and num_errors_row > max_errors:
                logger.warning('too many errors, abort')
                break
            # clamp the exponent: 1.1 ** 7448 raises OverflowError and kills the loop
            await asyncio.sleep(min(1.1 ** min(num_errors_row, 100), max_backoff))
        await asyncio.sleep(period)
