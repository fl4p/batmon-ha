import math
import time
from copy import copy
from typing import List, Dict, Optional

MIN_VALUE_EXPIRY = 20


class DeviceInfo:
    def __init__(self, mnf: str, model: str, hw_version: Optional[str], sw_version: Optional[str], name: Optional[str],
                 sn: Optional[str] = None):
        self.mnf = mnf
        self.model = model
        self.hw_version = hw_version
        self.sw_version = sw_version
        self.name = name
        self.sn = sn

    def __str__(self):
        s = f'DeviceInfo({self.model},hw-{self.hw_version},sw-{self.sw_version}'
        if self.name:
            s += ',' + self.name
        if self.sn:
            s += ',#' + self.sn
        return s + ')'


class PowerMonitorSample:
    # Todo this is a draft
    def __init__(self, voltage, current, power=math.nan, total_energy=math.nan):
        pass


class BmsSample:
    def __init__(self, voltage, current, power=math.nan,
                 charge=math.nan, capacity=math.nan, cycle_capacity=math.nan,
                 num_cycles=math.nan, soc=math.nan,
                 balance_current=math.nan,
                 temperatures: List[float] = None,
                 mos_temperature: float = math.nan,
                 switches: Optional[Dict[str, bool]] = None,
                 uptime=math.nan, timestamp: Optional[float] = None):
        """

        :param voltage:
        :param current: Current out of the battery (negative=charging, positive=discharging)
        :param charge: The charge available in Ah, aka remaining capacity, between 0 and `capacity`
        :param capacity: The capacity of the battery in Ah
        :param cycle_capacity: Total absolute charge meter (coulomb counter). Increases during charge and discharge. Can tell you the battery cycles (num_cycles = cycle_capacity/2/capacity). A better name would be cycle_charge. This is not well defined.
        :param num_cycles:
        :param soc: in % (0-100)
        :param balance_current:
        :param temperatures:
        :param mos_temperature:
        :param uptime: BMS uptime in seconds
        :param timestamp: seconds since epoch (unix timestamp from time.time())
        """
        self.voltage: float = voltage
        self.current: float = current or 0  # -
        self._power = power  # 0 -> +0
        self.balance_current = balance_current

        # infer soc from capacity if soc is nan or type(soc)==int (for higher precision)
        if capacity > 0 and (math.isnan(soc) or (isinstance(soc, int) and charge > 0)):
            soc = round(charge / capacity * 100, 2)
        elif math.isnan(capacity) and soc > .2:
            capacity = round(charge / soc * 100)

        # assert math.isfinite(soc)

        self.charge: float = charge
        self.capacity: float = capacity
        self.soc: float = soc
        self.cycle_capacity: float = cycle_capacity
        self.num_cycles: float = num_cycles
        self.temperatures = temperatures
        self.mos_temperature = mos_temperature
        self.switches = switches
        self.uptime = uptime
        self.timestamp = timestamp or time.time()

        self.num_samples = 0

        if switches:
            assert all(map(lambda x: isinstance(x, bool), switches.values())), "non-bool switches values %s" % switches

    @property
    def power(self):
        """
        :return: Power (P=U*I) in W
        """
        return (self.voltage * self.current) if math.isnan(self._power) else self._power

    def values(self):
        return {**self.__dict__, "power": self.power}

    def __str__(self):
        # noinspection PyStringFormat
        s = 'BmsSampl('
        if not math.isnan(self.soc):
            s += '%.1f%%,' % self.soc
        vals = self.values()
        s += 'U=%(voltage).1fV,I=%(current).2fA,P=%(power).0fW,' % vals
        if not math.isnan(self.charge):
            s += 'Q=%(charge).0f/%(capacity).0fAh,mos=%(mos_temperature).0f°C' % vals
        return s.rstrip(',') + ')'

    def invert_current(self):
        return self.multiply_current(-1)

    def multiply_current(self, x):
        res = copy(self)
        if res.current != 0:  # prevent -0 values
            res.current *= x
        if not math.isnan(res._power) and res._power != 0:
            res._power *= x
        return res
