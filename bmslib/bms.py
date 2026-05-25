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
                 charge=math.nan, capacity=math.nan, total_charge_throughput=math.nan,
                 num_cycles=math.nan, soc=math.nan,
                 soh=math.nan, aged_capacity=math.nan,
                 balance_current=math.nan,
                 temperatures: List[float] = None,
                 mos_temperature: float = math.nan,
                 switches: Optional[Dict[str, bool]] = None,
                 uptime=math.nan, timestamp: Optional[float] = None):
        """

        :param voltage:
        :param current: Current out of the battery (negative=charging, positive=discharging)
        :param charge: The charge available in Ah, aka remaining capacity, between 0 and `capacity`
        :param capacity: Nominal pack capacity in Ah, as configured by the user
        :param total_charge_throughput: Lifetime accumulated charge throughput in Ah, ``∫|I|dt`` — increases during both charge and discharge. Equivalent cycles ≈ total_charge_throughput / 2 / capacity. (Previously named ``cycle_capacity``, which was misleading: it's a charge counter in Ah, not a capacity.)
        :param num_cycles:
        :param soc: in % (0-100)
        :param soh: State of Health in % (0-100). Ratio of present effective capacity to nominal capacity. Reported directly by most BMSes.
        :param aged_capacity: Present effective pack capacity in Ah after aging (``capacity × soh/100``). If not provided but ``soh`` and ``capacity`` are known, it's derived; if not provided but ``capacity`` is known and ``soh`` isn't, ``soh`` is derived from this instead. The BMS may report this slightly differently from the SOH-derived value (different rounding / calibration); decoders should prefer the BMS-reported value when available.
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

        # Derive SOH↔aged_capacity if only one was provided and capacity is known.
        # Both may also be set explicitly by decoders (e.g. JK BMS exposes both
        # separately and they don't perfectly round-trip the formula).
        if capacity > 0:
            if math.isnan(aged_capacity) and not math.isnan(soh):
                aged_capacity = capacity * soh / 100
            elif math.isnan(soh) and not math.isnan(aged_capacity):
                soh = aged_capacity / capacity * 100

        self.charge: float = charge
        self.capacity: float = capacity
        self.soc: float = soc
        self.soh: float = soh
        self.aged_capacity: float = aged_capacity
        self.total_charge_throughput: float = total_charge_throughput
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
