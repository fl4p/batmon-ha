"""Module to support ABC BMS."""

from collections.abc import Callable
import contextlib
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
    KEY_TEMP_SENS,
    KEY_TEMP_VALUE,
)

from .basebms import BaseBMS, BMSsample, crc8


class BMS(BaseBMS):
    """ABC battery class implementation."""

    BAT_TIMEOUT = 1
    _HEAD_CMD: Final[int] = 0xEE
    _HEAD_RESP: Final[bytes] = b"\xCC"
    _INFO_LEN: Final[int] = 0x14
    _EXP_REPLY: Final[dict[int, list[int]]] = {  # wait for these replies
        0xC0: [0xF1],
        0xC1: [0xF0, 0xF2],
        0xC2: [0xF0, 0xF3, 0xF4],
        0xC3: [0xF5, 0xF6, 0xF7, 0xF8, 0xFA],
        0xC4: [0xF9] * 2,  # 4 cells per message
    }
    _FIELDS: Final[
        list[tuple[str, int, int, int, bool, Callable[[int], int | float]]]
    ] = [
        (KEY_TEMP_SENS, 0xF2, 4, 1, False, lambda x: x),
        (ATTR_VOLTAGE, 0xF0, 2, 3, False, lambda x: float(x / 1000)),
        (ATTR_CURRENT, 0xF0, 5, 3, True, lambda x: float(x / 1000)),
        # (KEY_DESIGN_CAP, 0xF0, 8, 3, False, lambda x: float(x / 1000)),
        (ATTR_BATTERY_LEVEL, 0xF0, 16, 1, False, lambda x: x),
        (ATTR_CYCLE_CHRG, 0xF0, 11, 3, False, lambda x: float(x / 1000)),
        (ATTR_CYCLES, 0xF0, 14, 2, False, lambda x: x),
        (  # only first bit per byte is used
            KEY_PROBLEM,
            0xF9,
            2,
            16,
            False,
            lambda x: sum(((x >> (i * 8)) & 1) << i for i in range(16)),
        ),
    ]
    _RESPS: Final[set[int]] = {field[1] for field in _FIELDS} | {
        field[1] for field in _FIELDS
    }

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._exp_reply: list[int] = []

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": normalize_uuid_str("fff0"),
                "connectable": True,
            }
            for pattern in ("SOK-*", "ABC-*")  # "NB-*", "Hoover",
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Chunguang Song", "model": "ABC BMS"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("ffe0")]

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
        self._log.debug("RX BLE data: %s", data)

        if not data.startswith(BMS._HEAD_RESP):
            self._log.debug("Incorrect frame start")
            return

        if len(data) != BMS._INFO_LEN:
            self._log.debug("Incorrect frame length")
            return

        if (crc := crc8(data[:-1])) != data[-1]:
            self._log.debug("invalid checksum 0x%X != 0x%X", data[-1], crc)
            return

        if data[1] == 0xF4 and 0xF4 in self._data_final:
            # expand cell voltage frame with all parts
            self._data_final[0xF4] = bytearray(self._data_final[0xF4][:-2] + data[2:])
        else:
            self._data_final[data[1]] = data.copy()

        if data[1] in self._exp_reply:
            self._exp_reply.remove(data[1])

        if not self._exp_reply:  # check if all expected replies are received
            self._data_event.set()

    @staticmethod
    def _cmd(cmd: bytes) -> bytes:
        """Assemble a ABC BMS command."""
        frame = bytearray([BMS._HEAD_CMD, cmd[0], 0x00, 0x00, 0x00])
        frame += bytes([crc8(frame)])
        return frame

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        """Return cell voltages from status message."""
        return {
            f"{KEY_CELL_VOLTAGE}{data[2+idx*4]-1}": int.from_bytes(
                data[3 + idx * 4 : 6 + idx * 4], byteorder="little", signed=False
            )
            / 1000
            for idx in range(4 * (len(data) - 4) // 16)
        }

    @staticmethod
    def _temp_sensors(data: bytearray, sensors: int) -> dict[str, float]:
        return {
            f"{KEY_TEMP_VALUE}{idx}": int.from_bytes(
                data[5 + idx : 6 + idx], byteorder="little", signed=True
            )
            for idx in range(sensors)
        }

    @staticmethod
    def _decode_data(data: dict[int, bytearray]) -> dict[str, int | float]:
        return {
            key: func(
                int.from_bytes(
                    data[cmd][idx : idx + size],
                    byteorder="little",
                    signed=sign,
                )
            )
            for key, cmd, idx, size, sign, func in BMS._FIELDS
            if cmd in data
        }

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        self._data_final.clear()
        for cmd in (0xC4, 0xC2, 0xC1):
            self._exp_reply = BMS._EXP_REPLY[cmd].copy()
            with contextlib.suppress(TimeoutError):
                await self._await_reply(BMS._cmd(bytes([cmd])))

        # check all repsonses are here, 0xF9 is not mandatory
        if not BMS._RESPS.issubset(set(self._data_final.keys()) | {0xF9}):
            self._log.debug("Incomplete data set %s", self._data_final.keys())
            raise TimeoutError("BMS data incomplete.")

        result: BMSsample = BMS._decode_data(self._data_final)
        return (
            result
            | BMS._cell_voltages(self._data_final[0xF4])
            | BMS._temp_sensors(
                self._data_final[0xF2], int(result.get(KEY_TEMP_SENS, 0))
            )
        )
