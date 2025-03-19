"""Module to support Dummy BMS."""

from collections.abc import Callable
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
    ATTR_CYCLES,
    ATTR_DELTA_VOLTAGE,
    ATTR_POWER,
    ATTR_RUNTIME,
    ATTR_TEMPERATURE,
    ATTR_VOLTAGE,
    KEY_CELL_VOLTAGE,
    KEY_PROBLEM,
    KEY_TEMP_VALUE,
)

from .basebms import BaseBMS, BMSsample, crc_sum


class BMS(BaseBMS):
    """Dummy battery class implementation."""

    CRC_POS: Final[int] = -1  # last byte
    HEAD_LEN: Final[int] = 3
    MAX_CELLS: Final[int] = 16
    MAX_TEMP: Final[int] = 5
    _FIELDS: Final[list[tuple[str, int, int, bool, Callable[[int], int | float]]]] = [
        (ATTR_VOLTAGE, 12, 2, False, lambda x: float(x / 1000)),
        (ATTR_CURRENT, 48, 4, True, lambda x: float(x / 1000)),
        (ATTR_BATTERY_LEVEL, 90, 2, False, lambda x: x),
        (ATTR_CYCLE_CHRG, 62, 2, False, lambda x: float(x / 100)),
        (ATTR_CYCLES, 96, 4, False, lambda x: x),
        (KEY_PROBLEM, 76, 4, False, lambda x: x),
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "service_uuid": BMS.uuid_services()[0],
                "manufacturer_id": 0x585A,
                "connectable": True,
            }
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Redodo", "model": "Bluetooth battery"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("ffe0")]  # change service UUID here!

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "ffe1"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "ffe2"

    @staticmethod
    def _calc_values() -> set[str]:
        return {
            ATTR_BATTERY_CHARGING,
            ATTR_DELTA_VOLTAGE,
            ATTR_CYCLE_CAP,
            ATTR_POWER,
            ATTR_RUNTIME,
            ATTR_TEMPERATURE,
        }  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        self._log.debug("RX BLE data: %s", data)

        if len(data) < 3 or not data.startswith(b"\x00\x00"):
            self._log.debug("incorrect SOF.")
            return

        if len(data) != data[2] + BMS.HEAD_LEN + 1:  # add header length and CRC
            self._log.debug("incorrect frame length (%i)", len(data))
            return

        if (crc := crc_sum(data[: BMS.CRC_POS])) != data[BMS.CRC_POS]:
            self._log.debug(
                "invalid checksum 0x%X != 0x%X", data[len(data) + BMS.CRC_POS], crc
            )
            return

        self._data = data
        self._data_event.set()

    @staticmethod
    def _cell_voltages(data: bytearray, cells: int) -> dict[str, float]:
        """Return cell voltages from status message."""
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": value / 1000
            for idx in range(cells)
            if (
                value := int.from_bytes(
                    data[16 + 2 * idx : 16 + 2 * idx + 2],
                    byteorder="little",
                )
            )
        }

    @staticmethod
    def _temp_sensors(data: bytearray, sensors: int) -> dict[str, int]:
        return {
            f"{KEY_TEMP_VALUE}{idx}": value
            for idx in range(sensors)
            if (
                value := int.from_bytes(
                    data[52 + idx * 2 : 54 + idx * 2],
                    byteorder="little",
                    signed=True,
                )
            )
        }

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        await self._await_reply(b"\x00\x00\x04\x01\x13\x55\xaa\x17")

        return (
            {
                key: func(
                    int.from_bytes(
                        self._data[idx : idx + size], byteorder="little", signed=sign
                    )
                )
                for key, idx, size, sign, func in BMS._FIELDS
            }
            | BMS._cell_voltages(self._data, BMS.MAX_CELLS)
            | BMS._temp_sensors(self._data, BMS.MAX_TEMP)
        )
