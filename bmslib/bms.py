import math
from copy import copy
from typing import List


class DeviceInfo():
    def __init__(self, model, hw_version, sw_version, name):
        self.model = model
        self.hw_version = hw_version
        self.sw_version = sw_version
        self.name = name

class BmsSample:
    def __init__(self, voltage, current,
                 charge=math.nan, capacity=math.nan, cycle_capacity=math.nan,
                 num_cycles=math.nan, soc=math.nan,
                 balance_current=math.nan,
                 temperatures: List[float] = None,
                 mos_temperature=math.nan,):
        """

        :param voltage:
        :param current: Current out of the battery (negative=charging, positive=discharging)
        :param charge:
        :param capacity:
        :param num_cycles:
        :param soc:
        :param balance_current:
        :param temperatures:
        :param mos_temperature:
        """
        self.voltage: float = voltage
        self.current: float = current
        self.balance_current = balance_current

        if math.isnan(soc):
            soc = round(charge / capacity * 100, 2)
        else:
            if math.isnan(capacity):
                capacity = round(charge / soc * 100)

        assert math.isfinite(soc)

        self.charge: float = charge
        self.capacity : float = capacity
        self.cycle_capacity : float = cycle_capacity
        self.num_cycles: float = num_cycles
        self.temperatures = temperatures
        self.mos_temperature = mos_temperature

    @property
    def power(self):
        return round(self.voltage * self.current, 2)

    @property
    def soc(self):
        return round(self.charge / self.capacity * 100, 2)

    def __str__(self):
        # noinspection PyStringFormat
        return 'BmsSample(U=%(voltage).1fV,I=%(current).2fA,P=%(power).0fW,q=%(charge).1fAh/%(capacity).0f,mos=%(mos_temperature).1f°C)' % {
            **self.__dict__,
            "power": self.power
        }

    def invert_current(self):
        res = copy(self)
        res.current *= -1
        return res

