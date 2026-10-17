"""#417: aiobmsble temp_values are published per probe, not only as the mean."""
import math
from dataclasses import dataclass
from enum import IntEnum

from bmslib.models.BLE_BMS_wrap import split_temperatures


class T(IntEnum):
    # mirrors aiobmsble 0.27 TempSensor.T
    GENERIC = 0
    CELL = 1
    MOSFET = 4
    AMBIENT = 8


@dataclass(frozen=True)
class TempSensor:
    value: float
    type: T = T.GENERIC

    def __float__(self):
        return float(self.value)


def test_seplos_issue_417_sample():
    # temp_values from the reporter's log: 4 cells, ambient, MOSFET
    s = dict(temperature=23.567, temp_values=[
        TempSensor(23.1, T.CELL), TempSensor(22.8, T.CELL), TempSensor(22.9, T.CELL),
        TempSensor(22.6, T.CELL), TempSensor(26.9, T.AMBIENT), TempSensor(23.1, T.MOSFET)])
    temps, mos = split_temperatures(s)
    assert temps == [23.1, 22.8, 22.9, 22.6, 26.9]
    assert all(type(t) is float for t in temps)
    assert mos == 23.1


def test_only_first_mosfet_is_mos_temperature():
    temps, mos = split_temperatures(dict(temp_values=[
        TempSensor(30.0, T.MOSFET), TempSensor(20.0, T.CELL), TempSensor(31.0, T.MOSFET)]))
    assert mos == 30.0
    assert temps == [20.0, 31.0]


def test_plain_numbers_from_older_aiobmsble():
    temps, mos = split_temperatures(dict(temperature=21.5, temp_values=[21, 22.0]))
    assert temps == [21.0, 22.0]
    assert math.isnan(mos)


def test_mean_only_falls_back_to_temperature():
    temps, mos = split_temperatures(dict(temperature=19.5))
    assert temps == [19.5]
    assert math.isnan(mos)


def test_no_temperature_at_all():
    temps, mos = split_temperatures(dict(voltage=13.3))
    assert temps == []
    assert math.isnan(mos)
