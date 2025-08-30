"""Module to support Felicity BMS."""

from collections.abc import Callable
from json import JSONDecodeError, loads
from typing import Any, Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSsample, BMSvalue


class BMS(BaseBMS):
    """Felicity BMS implementation."""

    _HEAD: Final[bytes] = b"{"
    _TAIL: Final[bytes] = b"}"
    _CMD_PRE: Final[bytes] = b"wifilocalMonitor:"  # CMD prefix
    _CMD_BI: Final[bytes] = b"get dev basice infor"
    _CMD_DT: Final[bytes] = b"get Date"
    _CMD_RT: Final[bytes] = b"get dev real infor"
    _FIELDS: Final[list[tuple[BMSvalue, str, Callable[[list], Any]]]] = [
        ("voltage", "Batt", lambda x: x[0][0] / 1000),
        ("current", "Batt", lambda x: x[1][0] / 10),
        (
            "cycle_charge",
            "BatsocList",
            lambda x: (int(x[0][0]) * int(x[0][2])) / 1e7,
        ),
        ("battery_level", "BatsocList", lambda x: x[0][0] / 100),
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)
        self._data_final: dict = {}

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {"local_name": pattern, "connectable": True} for pattern in ("F07*", "F10*")
        ]

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
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "battery_charging",
                "cycle_capacity",
                "delta_voltage",
                "power",
                "runtime",
                "temperature",
            }
        )  # calculate further values from BMS provided set ones

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
    def _conv_data(data: dict) -> BMSsample:
        result: BMSsample = {}
        for key, itm, func in BMS._FIELDS:
            result[key] = func(data.get(itm, []))
        return result

    @staticmethod
    def _conv_cells(data: dict) -> list[float]:
        return [(value / 1000) for value in data.get("BatcelList", [])[0]]

    @staticmethod
    def _conv_temp(data: dict) -> list[float]:
        return [
            (value / 10) for value in data.get("BtemList", [])[0] if value != 0x7FFF
        ]

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        await self._await_reply(BMS._CMD_PRE + BMS._CMD_RT)

        return (
            BMS._conv_data(self._data_final)
            | {"temp_values": BMS._conv_temp(self._data_final)}
            | {"cell_voltages": BMS._conv_cells(self._data_final)}
            | {
                "problem_code": int(
                    self._data_final.get("Bwarn", 0) + self._data_final.get("Bfault", 0)
                )
            }
        )
