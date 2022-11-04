import math
from copy import copy
from typing import List, Dict, Optional

MIN_VALUE_EXPIRY = 20


class DeviceInfo():
    def __init__(self, model, hw_version, sw_version, name, sn):
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


class BmsSample:
    def __init__(self, voltage, current,
                 charge=math.nan, capacity=math.nan, cycle_capacity=math.nan,
                 num_cycles=math.nan, soc=math.nan,
                 balance_current=math.nan,
                 temperatures: List[float] = None,
                 mos_temperature=math.nan,
                 switches: Optional[Dict[str, bool]] = None,
                 uptime=math.nan):
        """

        :param voltage:
        :param current: Current out of the battery (negative=charging, positive=discharging)
        :param charge:
        :param capacity:
        :param num_cycles:
        :param soc: in % (0-100)
        :param balance_current:
        :param temperatures:
        :param mos_temperature:
        :param uptime BMS uptime in seconds
        """
        self.voltage: float = voltage
        self.current: float = current
        self.balance_current = balance_current

        if math.isnan(soc):
            soc = round(charge / capacity * 100, 2)
        else:
            if math.isnan(capacity) and soc > .2:
                capacity = round(charge / soc * 100)

        assert math.isfinite(soc)

        self.charge: float = charge
        self.capacity: float = capacity
        self.soc: float = soc
        self.cycle_capacity: float = cycle_capacity
        self.num_cycles: float = num_cycles
        self.temperatures = temperatures
        self.mos_temperature = mos_temperature
        self.switches = switches
        self.uptime = uptime

        if switches:
            assert all(map(lambda x: isinstance(x, bool), switches.values())), "non-bool switches values %s" % switches

    @property
    def power(self):
        return round(self.voltage * self.current, 2)

    def __str__(self):
        # noinspection PyStringFormat
        return 'BmsSample(U=%(voltage).1fV,I=%(current).2fA,P=%(power).0fW,q=%(charge).1fAh/%(capacity).1f,mos=%(mos_temperature).1f°C)' % {
            **self.__dict__,
            "power": self.power
        }

    def invert_current(self):
        res = copy(self)
        if res.current != 0:  # prevent -0 values
            res.current *= -1
        return res
