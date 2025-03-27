"""Module to support RoyPow BMS."""

from collections.abc import Callable
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

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
    KEY_TEMP_SENS,
    KEY_TEMP_VALUE,
)

from .basebms import BaseBMS, BMSsample


class BMS(BaseBMS):
    """RoyPow battery class implementation."""

    _HEAD: Final[bytes] = b"\xea\xd1\x01"
    _TAIL: Final[int] = 0xF5
    _BT_MODULE_MSG: Final[bytes] = b"AT+STAT\r\n"  # AT cmd from BLE module
    _MIN_LEN: Final[int] = len(_HEAD) + 1
    _FIELDS: Final[
        list[tuple[str, int, int, int, bool, Callable[[int], int | float]]]
    ] = [
        (ATTR_BATTERY_LEVEL, 0x4, 7, 1, False, lambda x: x),
        (ATTR_VOLTAGE, 0x4, 47, 2, False, lambda x: float(x / 100)),
        (
            ATTR_CURRENT,
            0x3,
            6,
            3,
            False,
            lambda x: float((x & 0xFFFF) * (-1 if (x >> 16) & 0x1 else 1) / 100),
        ),
        (KEY_PROBLEM, 0x3, 9, 3, False, lambda x: x),
        (
            ATTR_CYCLE_CHRG,
            0x4,
            24,
            4,
            False,
            lambda x: float(
                ((x & 0xFFFF0000) | (x & 0xFF00) >> 8 | (x & 0xFF) << 8) / 1000
            ),
        ),
        (ATTR_RUNTIME, 0x4, 30, 2, False, lambda x: x * 60),
        (KEY_TEMP_SENS, 0x3, 13, 1, False, lambda x: x),
        (ATTR_CYCLES, 0x4, 9, 2, False, lambda x: x),
    ]
    _CMDS: Final[set[int]] = set({field[1] for field in _FIELDS})

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._exp_len: int = 0

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "service_uuid": BMS.uuid_services()[0],
                "manufacturer_id": manufacturer_id,
                "connectable": True,
            }
            for manufacturer_id in (0x01A8, 0x0B31, 0x8AFB)
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "RoyPow", "model": "SmartBMS"}

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
        return "ffe1"

    @staticmethod
    def _calc_values() -> frozenset[str]:
        return frozenset(
            {
                ATTR_BATTERY_CHARGING,
                ATTR_CYCLE_CAP,
                ATTR_DELTA_VOLTAGE,
                ATTR_POWER,
                ATTR_TEMPERATURE,
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        if not (data := data.removeprefix(BMS._BT_MODULE_MSG)):
            self._log.debug("filtering AT cmd")
            return

        if data.startswith(BMS._HEAD) and not self._data.startswith(BMS._HEAD):
            self._exp_len = data[len(BMS._HEAD)]
            self._data.clear()

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        if not self._data.startswith(BMS._HEAD):
            self._data.clear()
            return

        # verify that data is long enough
        if len(self._data) < BMS._MIN_LEN + self._exp_len:
            return

        end_idx: Final[int] = BMS._MIN_LEN + self._exp_len - 1
        if self._data[end_idx] != BMS._TAIL:
            self._log.debug("incorrect EOF: %s", self._data)
            self._data.clear()
            return

        if (crc := BMS._crc(self._data[len(BMS._HEAD) : end_idx - 1])) != self._data[
            end_idx - 1
        ]:
            self._log.debug(
                "invalid checksum 0x%X != 0x%X", self._data[end_idx - 1], crc
            )
            self._data.clear()
            return

        self._data_final[self._data[5]] = self._data.copy()
        self._data.clear()
        self._data_event.set()

    @staticmethod
    def _decode_data(data: dict[int, bytearray]) -> dict[str, int | float]:
        return {
            key: func(
                int.from_bytes(
                    data[cmd][idx : idx + size],
                    byteorder="big",
                    signed=sign,
                )
            )
            for key, cmd, idx, size, sign, func in BMS._FIELDS
            if cmd in data
        }

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        """Return cell voltages from status message."""
        cells: Final[int] = max(0, (len(data) - 11) // 2)
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": value / 1000
            for idx in range(cells)
            if (
                value := int.from_bytes(
                    data[9 + 2 * idx : 11 + 2 * idx],
                    byteorder="big",
                )
            )
        }

    @staticmethod
    def _temp_sensors(data: bytearray, sensors: int) -> dict[str, int]:
        return {f"{KEY_TEMP_VALUE}{idx}": data[14 + idx] - 40 for idx in range(sensors)}

    @staticmethod
    def _crc(frame: bytes) -> int:
        """Calculate XOR of all frame bytes."""
        crc: int = 0
        for b in frame:
            crc ^= b
        return crc

    @staticmethod
    def _cmd(cmd: bytes) -> bytes:
        """Assemble a RoyPow BMS command."""
        data: Final[bytes] = bytes([len(cmd) + 2, *cmd])
        return bytes([*BMS._HEAD, *data, BMS._crc(data), BMS._TAIL])

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        self._data.clear()
        self._data_final.clear()
        for cmd in range(2, 5):
            await self._await_reply(BMS._cmd(bytes([0xFF, cmd])))

        result: BMSsample = BMS._decode_data(self._data_final)

        # remove remaining runtime if battery is charging
        if result.get(ATTR_RUNTIME) == 0xFFFF * 60:
            result.pop(ATTR_RUNTIME, None)

        return (
            result
            | BMS._cell_voltages(self._data_final.get(0x2, bytearray()))
            | BMS._temp_sensors(
                self._data_final.get(0x3, bytearray()),
                int(result.get(KEY_TEMP_SENS, 0)),
            )
        )
