import datetime
import time
from typing import Optional

import paho.mqtt.client

import bmslib.bt
from bmslib.bms import DeviceInfo
from bmslib.pwmath import Integrator, DiffAbsSum
from bmslib.util import get_logger
from mqtt_util import publish_sample, publish_cell_voltages, publish_temperatures, publish_hass_discovery, \
    subscribe_switches, mqtt_single_out, round_to_n

logger = get_logger(verbose=False)


class BmsSampler():

    def __init__(self, bms: bmslib.bt.BtBms, mqtt_client: paho.mqtt.client.Client, dt_max_seconds, expire_after_seconds,
                 invert_current=False, meter_state=None, publish_period=None):
        self.bms = bms
        self.mqtt_client = mqtt_client
        self.invert_current = invert_current
        self.expire_after_seconds = expire_after_seconds
        self.device_info: Optional[DeviceInfo] = None
        self.num_samples = 0
        self.publish_period = publish_period
        self._t_pub = 0

        dx_max = dt_max_seconds / 3600
        self.current_integrator = Integrator(name="total_charge", dx_max=dx_max)
        self.power_integrator = Integrator(name="total_energy", dx_max=dx_max)
        self.power_integrator_discharge = Integrator(name="total_energy_discharge", dx_max=dx_max)
        self.power_integrator_charge = Integrator(name="total_energy_charge", dx_max=dx_max)
        self.cycle_integrator = DiffAbsSum(name="total_cycles", dx_max=dx_max, dy_max=0.1)
        self.charge_integrator = DiffAbsSum(name="total_abs_diff_charge", dx_max=dx_max, dy_max=0.5) # TODO normalize dy_max to capacity

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
        except:
            dd = self.bms.debug_data()
            if dd:
                logger.info("bms debug data: %s", dd)
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

                # discharging P>0
                self.power_integrator_charge += (t_hour, abs(min(0, sample.power))  * 1e-3)
                self.power_integrator_discharge += (t_hour, abs(max(0, sample.power)) * 1e-3)

                if self.invert_current:
                    sample = sample.invert_current()

                self.current_integrator += (t_hour, sample.current)
                self.power_integrator += (t_hour, sample.power * 1e-3)

                self.cycle_integrator += (t_hour, sample.soc * (0.01 / 2)) # SoC 100->0 is a half cycle
                self.charge_integrator += (t_hour, sample.charge)

                if self.num_samples == 0 and sample.switches:
                    logger.info("%s subscribing for %s switch change", bms.name, sample.switches)
                    subscribe_switches(mqtt_client, device_topic=bms.name, bms=bms, switches=sample.switches.keys())

                publish_discovery = (self.num_samples % 60) == 0

                if publish_discovery or not self.publish_period or (t_now - self._t_pub) >= self.publish_period:
                    self._t_pub = t_now

                    publish_sample(mqtt_client, device_topic=bms.name, sample=sample)
                    logger.info('%s result@%s %s', bms.name, datetime.datetime.now().isoformat(), sample)

                    self.publish_meters()

                    voltages = await bms.fetch_voltages()
                    publish_cell_voltages(mqtt_client, device_topic=bms.name, voltages=voltages)

                    temperatures = sample.temperatures or await bms.fetch_temperatures()
                    publish_temperatures(mqtt_client, device_topic=bms.name, temperatures=temperatures)
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
                        mqtt_client, device_topic=bms.name, expire_after_seconds=self.expire_after_seconds,
                        sample=sample,
                        num_cells=len(voltages), num_temp_sensors=len(temperatures),
                        device_info=self.device_info,
                    )

                self.num_samples += 1
                t_disc = time.time()

        except Exception as ex:
            logger.error('%s error: %s', bms.name, str(ex) or str(type(ex)))
            raise

        dt_conn = t_fetch - t_conn
        dt_fetch = t_disc - t_fetch
        if self.bms.verbose_log or max(dt_conn, dt_fetch) > 1:
            logger.info('%s times: connect=%.2fs fetch=%.2fs', bms, dt_conn, dt_fetch)

    def publish_meters(self):
        device_topic = self.bms.name
        for meter in self.meters:
            topic = f"{device_topic}/meter/{meter.name}"
            s = round_to_n(meter.get(), 4)
            mqtt_single_out(self.mqtt_client, topic, s)
