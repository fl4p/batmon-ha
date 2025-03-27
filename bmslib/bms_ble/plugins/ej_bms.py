"""Module to support Dummy BMS."""

from collections.abc import Callable
from enum import IntEnum
from string import hexdigits
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from bmslib.bms_ble.const import (
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
)

from .basebms import BaseBMS, BMSsample


class Cmd(IntEnum):
    """BMS operation codes."""

    RT = 0x2
    CAP = 0x10


class BMS(BaseBMS):
    """Dummy battery class implementation."""

    _BT_MODULE_MSG: Final[bytes] = bytes([0x41, 0x54, 0x0D, 0x0A])  # BLE module message
    _HEAD: Final[bytes] = b"\x3a"
    _TAIL: Final[bytes] = b"\x7e"
    _MAX_CELLS: Final[int] = 16
    _FIELDS: Final[list[tuple[str, Cmd, int, int, Callable[[int], int | float]]]] = [
        (ATTR_CURRENT, Cmd.RT, 89, 8, lambda x: float((x >> 16) - (x & 0xFFFF)) / 100),
        (ATTR_BATTERY_LEVEL, Cmd.RT, 123, 2, lambda x: x),
        (ATTR_CYCLE_CHRG, Cmd.CAP, 15, 4, lambda x: float(x) / 10),
        (ATTR_TEMPERATURE, Cmd.RT, 97, 2, lambda x: x - 40),  # only 1st sensor relevant
        (ATTR_CYCLES, Cmd.RT, 115, 4, lambda x: x),
        (KEY_PROBLEM, Cmd.RT, 105, 4, lambda x: x & 0x0FFC),  # mask status bits
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: bytearray = bytearray()

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [  # Fliteboard, Electronix battery
            {"local_name": "libatt*", "manufacturer_id": 21320, "connectable": True},
            {"local_name": "LT-*", "manufacturer_id": 33384, "connectable": True},
            {"local_name": "L-12V???AH-*", "connectable": True},  # Lithtech Energy
            {"local_name": "LT-12V-*", "connectable": True},  # Lithtech Energy
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "E&J Technology", "model": "Smart BMS"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return ["6e400001-b5a3-f393-e0a9-e50e24dcca9e"]

    @staticmethod
    def uuid_rx() -> str:
        """Return 128-bit UUID of characteristic that provides notification/read property."""
        return "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

    @staticmethod
    def uuid_tx() -> str:
        """Return 128-bit UUID of characteristic that provides write property."""
        return "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

    @staticmethod
    def _calc_values() -> frozenset[str]:
        return frozenset(
            {
                ATTR_BATTERY_CHARGING,
                ATTR_CYCLE_CAP,
                ATTR_DELTA_VOLTAGE,
                ATTR_POWER,
                ATTR_RUNTIME,
                ATTR_VOLTAGE,
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""

        if data.startswith(BMS._BT_MODULE_MSG):
            self._log.debug("filtering AT cmd")
            if not (data := data.removeprefix(BMS._BT_MODULE_MSG)):
                return

        if data.startswith(BMS._HEAD):  # check for beginning of frame
            self._data.clear()

        self._data += data

        self._log.debug(
            "%s: RX BLE data (%s): %s",
            self._ble_device.name,
            "start" if data == self._data else "cnt.",
            data,
        )

        exp_frame_len: Final[int] = (
            int(self._data[7:11], 16)
            if len(self._data) > 10
            and all(chr(c) in hexdigits for c in self._data[7:11])
            else 0xFFFF
        )

        if not self._data.startswith(BMS._HEAD) or (
            not self._data.endswith(BMS._TAIL) and len(self._data) < exp_frame_len
        ):
            return

        if not self._data.endswith(BMS._TAIL):
            self._log.debug("incorrect EOF: %s", data)
            self._data.clear()
            return

        if not all(chr(c) in hexdigits for c in self._data[1:-1]):
            self._log.debug("incorrect frame encoding.")
            self._data.clear()
            return

        if len(self._data) != exp_frame_len:
            self._log.debug(
                "incorrect frame length %i != %i",
                len(self._data),
                exp_frame_len,
            )
            self._data.clear()
            return

        if (crc := BMS._crc(self._data[1:-3])) != int(self._data[-3:-1], 16):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X", int(self._data[-3:-1], 16), crc
            )
            self._data.clear()
            return

        self._log.debug(
            "address: 0x%X, commnad 0x%X, version: 0x%X, length: 0x%X",
            int(self._data[1:3], 16),
            int(self._data[3:5], 16) & 0x7F,
            int(self._data[5:7], 16),
            len(self._data),
        )
        self._data_final = self._data.copy()
        self._data_event.set()

    @staticmethod
    def _crc(data: bytes) -> int:
        return (sum(data) ^ 0xFF) & 0xFF

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        """Return cell voltages from status message."""
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": int(data[25 + 4 * idx : 25 + 4 * idx + 4], 16)
            / 1000
            for idx in range(BMS._MAX_CELLS)
            if int(data[25 + 4 * idx : 25 + 4 * idx + 4], 16)
        }

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        raw_data: dict[int, bytearray] = {}

        # query real-time information and capacity
        for cmd in (b":000250000E03~", b":001031000E05~"):
            await self._await_reply(cmd)
            rsp: int = int(self._data_final[3:5], 16) & 0x7F
            raw_data[rsp] = self._data_final
            if rsp == Cmd.RT and len(self._data_final) == 0x8C:
                # handle metrisun version
                self._log.debug("single frame protocol detected")
                raw_data[Cmd.CAP] = bytearray(15) + self._data_final[125:]
                break

        if len(raw_data) != len(list(Cmd)) or not all(
            len(value) > 0 for value in raw_data.values()
        ):
            return {}

        return {
            key: func(int(raw_data[cmd.value][idx : idx + size], 16))
            for key, cmd, idx, size, func in BMS._FIELDS
        } | self._cell_voltages(raw_data[Cmd.RT])
