import random
import re
import time
from typing import Optional

import paho.mqtt.client

import bmslib.bt
from bmslib.algorithm import create_algorithm, BatterySwitches
from bmslib.bms import DeviceInfo
from bmslib.group import BmsGroup, GroupNotReady
from bmslib.pwmath import Integrator, DiffAbsSum
from bmslib.util import get_logger
from mqtt_util import publish_sample, publish_cell_voltages, publish_temperatures, publish_hass_discovery, \
    subscribe_switches, mqtt_single_out, round_to_n

logger = get_logger(verbose=False)


class BmsSampler:

    def __init__(self, bms: bmslib.bt.BtBms, mqtt_client: paho.mqtt.client.Client, dt_max_seconds, expire_after_seconds,
                 invert_current=False, meter_state=None, publish_period=None, algorithms: Optional[list] = None,
                 current_calibration_factor = 1.0,
                 bms_group: Optional[BmsGroup] = None):
        self.bms = bms
        self.mqtt_topic_prefix = re.sub(r'[^\w_.-]', '_', bms.name)
        self.mqtt_client = mqtt_client
        self.invert_current = invert_current
        self.expire_after_seconds = expire_after_seconds
        self.device_info: Optional[DeviceInfo] = None
        self.num_samples = 0
        self.publish_period = publish_period
        self.bms_group = bms_group  # group, virtual, parent
        self.current_calibration_factor = current_calibration_factor

        self._t_pub = 0

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

    def get_meter_state(self):
        return {meter.name: dict(reading=meter.get()) for meter in self.meters}

    async def __call__(self):
        try:
            return await self.sample()
        except Exception:
            dd = self.bms.debug_data()
            if dd:
                logger.info("%s bms debug data: %s", self.bms.name, dd)
            if self.device_info:
                logger.info('%s device info: %s', self.bms.name, self.device_info)
            logger.info('Bleak version %s', bmslib.bt.bleak_version())
            raise

    async def sample(self):
        bms = self.bms
        mqtt_client = self.mqtt_client

        was_connected = bms.is_connected

        if not was_connected:
            logger.info('connecting bms %s', bms)

        t_conn = time.time()

        try:
            async with bms:
                if not was_connected:
                    logger.info('connected bms %s!', bms)

                t_fetch = time.time()

                sample = await bms.fetch()

                t_now = time.time()
                t_hour = t_now * (1 / 3600)

                if sample.timestamp < t_now - self.expire_after_seconds:
                    logger.warning('%s expired sample', bms.name)
                    return

                if self.bms_group:
                    self.bms_group.update(bms, sample)

                # discharging P>0
                self.power_integrator_charge += (t_hour, abs(min(0, sample.power)) * 1e-3)  # kWh
                self.power_integrator_discharge += (t_hour, abs(max(0, sample.power)) * 1e-3)  # kWh

                if self.invert_current:
                    sample = sample.invert_current()

                if self.current_calibration_factor and self.current_calibration_factor != 1:
                    sample = sample.multiply_current(self.current_calibration_factor)

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

                if self.num_samples == 0 and sample.switches:
                    logger.info("%s subscribing for %s switch change", bms.name, sample.switches)
                    subscribe_switches(mqtt_client, device_topic=self.mqtt_topic_prefix, bms=bms,
                                       switches=sample.switches.keys())

                publish_discovery = (self.num_samples % 60) == 0

                if publish_discovery or not self.publish_period or (t_now - self._t_pub) >= self.publish_period:
                    self._t_pub = t_now

                    publish_sample(mqtt_client, device_topic=self.mqtt_topic_prefix, sample=sample)
                    logger.info('%s: %s', bms.name, sample)

                    self.publish_meters()

                    voltages = await bms.fetch_voltages()
                    if self.bms_group:
                        self.bms_group.update_voltages(bms, voltages)
                    publish_cell_voltages(mqtt_client, device_topic=self.mqtt_topic_prefix, voltages=voltages)

                    temperatures = sample.temperatures or await bms.fetch_temperatures()
                    publish_temperatures(mqtt_client, device_topic=self.mqtt_topic_prefix, temperatures=temperatures)
                    if voltages or temperatures:
                        logger.info('%s volt=%s temp=%s', bms.name, ','.join(map(str, voltages)), temperatures)


                # publish home assistant discovery every 60 samples
                if publish_discovery:
                    if self.device_info is None:
                        try:
                            self.device_info = await bms.fetch_device_info()
                        except NotImplementedError:
                            pass
                        except Exception as e:
                            logger.warning('%s error fetching device info: %s', bms.name, e)
                    publish_hass_discovery(
                        mqtt_client, device_topic=self.mqtt_topic_prefix,
                        expire_after_seconds=self.expire_after_seconds,
                        sample=sample,
                        num_cells=len(voltages), num_temp_sensors=len(temperatures),
                        device_info=self.device_info,
                    )

                self.num_samples += 1
                t_disc = time.time()

        except GroupNotReady as ex:
            logger.error('%s group not ready: %s', bms.name, ex)
            return
        except Exception as ex:
            logger.error('%s error: %s', bms.name, str(ex) or str(type(ex)))
            raise

        dt_conn = t_fetch - t_conn
        dt_fetch = t_disc - t_fetch
        if self.bms.verbose_log or max(dt_conn, dt_fetch) > 1 or random.random() < 0.05:
            logger.info('%s times: connect=%.2fs fetch=%.2fs', bms, dt_conn, dt_fetch)

    def publish_meters(self):
        device_topic = self.mqtt_topic_prefix
        for meter in self.meters:
            topic = f"{device_topic}/meter/{meter.name}"
            s = round_to_n(meter.get(), 4)
            mqtt_single_out(self.mqtt_client, topic, s)
