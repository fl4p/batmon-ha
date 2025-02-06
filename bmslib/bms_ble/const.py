"""Constants for the BLE Battery Management System integration."""

import logging
from typing import Final

#from homeassistant.const import (  # noqa: F401
#    ATTR_BATTERY_CHARGING,
#    ATTR_BATTERY_LEVEL,
#    ATTR_TEMPERATURE,
#    ATTR_VOLTAGE,
#)

ATTR_BATTERY_CHARGING: Final = "battery_charging"
ATTR_BATTERY_LEVEL: Final = "battery_level"
ATTR_VOLTAGE: Final = "voltage"
ATTR_TEMPERATURE: Final = "temperature"


BMS_TYPES: Final[list[str]] = [
    "cbtpwr_bms",
    "daly_bms",
    "ective_bms",
    "ej_bms",
    "jbd_bms",
    "jikong_bms",
    "ogt_bms",
    "redodo_bms",
    "seplos_bms",
    "seplos_v2_bms",
    "tdt_bms",
    "dpwrcore_bms",  # only name filter
]  # available BMS types
DOMAIN: Final[str] = "bms_ble"
LOGGER: Final = logging.getLogger(__package__)
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
ATTR_RSSI: Final[str] = "rssi"  # [dBm]
ATTR_RUNTIME: Final[str] = "runtime"  # [s]
ATTR_TEMP_SENSORS: Final[str] = "temperature_sensors"  # [°C]

# temporary dictionary keys (do not change)
KEY_CELL_COUNT: Final[str] = "cell_count"  # [#]
KEY_CELL_VOLTAGE: Final[str] = "cell#"  # [V]
KEY_DESIGN_CAP: Final[str] = "design_capacity"  # [Ah]
KEY_PACK_COUNT: Final[str] = "pack_count"  # [#]
KEY_TEMP_SENS: Final[str] = "temp_sensors"  # [#]
KEY_TEMP_VALUE: Final[str] = "temp#"  # [°C]
