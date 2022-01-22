import math
from typing import List


class BmsSample:
    def __init__(self, voltage, current, charge=math.nan, charge_full=math.nan, num_cycles=math.nan, soc=math.nan,
                 temperatures: List[float] = None):
        self.voltage: float = voltage
        self.current: float = current

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

    @property
    def power(self):
        return round(self.voltage * self.current, 2)

    @property
    def soc(self):
        return self.charge / self.charge_full

    def __str__(self):
        # noinspection PyStringFormat
        return 'BmsSample(U=%(voltage).1fV,I=%(current).2fA,P=%(power).0fW,q=%(charge).1fAh/%(charge_full).0f)' % {
            **self.__dict__,
            "power": self.power
        }
