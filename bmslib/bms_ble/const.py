"""Constants for the BLE Battery Management System integration."""

import logging
from typing import Final

ATTR_BATTERY_CHARGING: Final = "battery_charging"
ATTR_BATTERY_LEVEL: Final = "battery_level"
ATTR_TEMPERATURE: Final = "temperature"
ATTR_VOLTAGE: Final = "voltage"


BMS_TYPES: Final[list[str]] = [
    "abc_bms",
    "braunpwr_bms",
    "ant_bms",
    "cbtpwr_bms",
    "cbtpwr_vb_bms",
    "daly_bms",
    "ecoworthy_bms",
    "ective_bms",
    "ej_bms",
    "jbd_bms",
    "jikong_bms",
    "neey_bms",  # active balancer
    "ogt_bms",
    "pro_bms",
    "redodo_bms",
    "renogy_bms",
    "renogy_pro_bms",
    "seplos_bms",
    "seplos_v2_bms",
    "roypow_bms",
    "tdt_bms",
    "dpwrcore_bms",  # **vvv** only name filter **vvv**
    "felicity_bms",
    "tianpwr_bms",
]  # available BMS types
DOMAIN: Final[str] = "bms_ble"
LOGGER: Final[logging.Logger] = logging.getLogger(__package__)
UPDATE_INTERVAL: Final[int] = 30  # [s]

# attributes (do not change)
ATTR_BALANCE_CUR: Final[str] = "balance_current"  # [A]
ATTR_CELL_VOLTAGES: Final[str] = "cell_voltages"  # [V]
ATTR_CURRENT: Final[str] = "current"  # [A]
ATTR_CYCLE_CAP: Final[str] = "cycle_capacity"  # [Wh]
ATTR_CYCLE_CHRG: Final[str] = "cycle_charge"  # [Ah]
ATTR_CYCLES: Final[str] = "cycles"  # [#]
ATTR_DELTA_VOLTAGE: Final[str] = "delta_voltage"  # [V]
ATTR_LQ: Final[str] = "link_quality"  # [%]
ATTR_POWER: Final[str] = "power"  # [W]
ATTR_PROBLEM: Final[str] = "problem"  # [bool]
ATTR_PROBLEM_CODE: Final[str] = "problem_code"  # [int]
ATTR_RSSI: Final[str] = "rssi"  # [dBm]
ATTR_RUNTIME: Final[str] = "runtime"  # [s]
ATTR_TEMP_SENSORS: Final[str] = "temperature_sensors"  # [°C]
