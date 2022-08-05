import math
from typing import List


class BmsSample:
    def __init__(self, voltage, current, charge=math.nan, charge_full=math.nan, num_cycles=math.nan, soc=math.nan,
                 balance_current=math.nan,
                 temperatures: List[float] = None,
                 mos_temperature=math.nan,):
        """

        :param voltage:
        :param current: Current out of the battery (negative=charging, positive=discharging)
        :param charge:
        :param charge_full:
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
            soc = round(charge / charge_full * 100, 2)
        else:
            if math.isnan(charge_full):
                charge_full = round(charge / soc * 100)

        assert math.isfinite(soc)

        self.charge: float = charge
        self.charge_full : float = charge_full
        self.num_cycles: float = num_cycles
        self.temperatures = temperatures
        self.mos_temperature = mos_temperature

    @property
    def power(self):
        return round(self.voltage * self.current, 2)

    @property
    def soc(self):
        return round(self.charge / self.charge_full * 100, 2)

    def __str__(self):
        # noinspection PyStringFormat
        return 'BmsSample(U=%(voltage).1fV,I=%(current).2fA,P=%(power).0fW,q=%(charge).1fAh/%(charge_full).0f,mos=%(mos_temperature).1f°C)' % {
            **self.__dict__,
            "power": self.power
        }
