"""Module to support Felicity BMS."""

from collections.abc import Callable
from json import JSONDecodeError, loads
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from custom_components.bms_ble.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_CURRENT,
    ATTR_CYCLE_CAP,
    ATTR_CYCLE_CHRG,
    # ATTR_CYCLES,
    ATTR_DELTA_VOLTAGE,
    ATTR_POWER,
    ATTR_RUNTIME,
    ATTR_TEMPERATURE,
    ATTR_VOLTAGE,
    KEY_CELL_VOLTAGE,
    KEY_PROBLEM,
    KEY_TEMP_VALUE,
)

from .basebms import BaseBMS, BMSsample


class BMS(BaseBMS):
    """Felicity battery class implementation."""

    _HEAD: Final[bytes] = b"{"
    _TAIL: Final[bytes] = b"}"
    _CMD_PRE: Final[bytes] = b"wifilocalMonitor:"  # CMD prefix
    _CMD_BI: Final[bytes] = b"get dev basice infor"
    _CMD_DT: Final[bytes] = b"get Date"
    _CMD_RT: Final[bytes] = b"get dev real infor"
    _FIELDS: Final[list[tuple[str, str, Callable[[list], int | float]]]] = [
        (ATTR_VOLTAGE, "Batt", lambda x: float(x[0][0] / 1000)),
        (ATTR_CURRENT, "Batt", lambda x: float(x[1][0] / 10)),
        (
            ATTR_CYCLE_CHRG,
            "BatsocList",
            lambda x: (int(x[0][0]) * int(x[0][2])) / 1e7,
        ),
        (ATTR_BATTERY_LEVEL, "BatsocList", lambda x: float(x[0][0] / 100)),
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: dict = {}

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [{"local_name": "F10*", "connectable": True}]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Felicity Solar", "model": "LiFePo4 battery"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("6e6f736a-4643-4d44-8fa9-0fafd005e455")]

    @staticmethod
    def uuid_rx() -> str:
        """Return 128-bit UUID of characteristic that provides notification/read property."""
        return "49535458-8341-43f4-a9d4-ec0e34729bb3"

    @staticmethod
    def uuid_tx() -> str:
        """Return 128-bit UUID of characteristic that provides write property."""
        return "49535258-184d-4bd9-bc61-20c647249616"

    @staticmethod
    def _calc_values() -> set[str]:
        return {
            ATTR_BATTERY_CHARGING,
            ATTR_CYCLE_CAP,
            ATTR_DELTA_VOLTAGE,
            ATTR_POWER,
            ATTR_RUNTIME,
            ATTR_TEMPERATURE,
        }  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""

        if data.startswith(BMS._HEAD):
            self._data = bytearray()

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        if not data.endswith(BMS._TAIL):
            return

        try:
            self._data_final = loads(self._data)
        except (JSONDecodeError, UnicodeDecodeError):
            self._log.debug("JSON decode error: %s", self._data)
            return

        if (ver := self._data_final.get("CommVer", 0)) != 1:
            self._log.debug("Unknown protocol version (%i)", ver)
            return

        self._data_event.set()

    @staticmethod
    def _decode_data(data: dict) -> dict[str, int | float]:
        return {key: func(data.get(itm, [])) for key, itm, func in BMS._FIELDS}

    @staticmethod
    def _cell_voltages(data: dict) -> dict[str, float]:
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": value / 1000
            for idx, value in enumerate(data.get("BatcelList", [])[0])
        }

    @staticmethod
    def _temp_sensors(data: dict) -> dict[str, float]:
        return {
            f"{KEY_TEMP_VALUE}{idx}": value / 10
            for idx, value in enumerate(data.get("BtemList", [])[0])
            if value != 0x7FFF
        }

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        await self._await_reply(BMS._CMD_PRE + BMS._CMD_RT)

        return (
            BMS._decode_data(self._data_final)
            | BMS._temp_sensors(self._data_final)
            | BMS._cell_voltages(self._data_final)
            | {
                KEY_PROBLEM: self._data_final.get("Bwarn", 0)
                + self._data_final.get("Bfault", 0)
            }
        )
