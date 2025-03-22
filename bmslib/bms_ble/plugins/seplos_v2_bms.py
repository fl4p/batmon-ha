"""Module to support Seplos V2 BMS."""

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
    KEY_CELL_COUNT,
    KEY_CELL_VOLTAGE,
    KEY_PACK_COUNT,
    KEY_PROBLEM,
    KEY_TEMP_SENS,
    KEY_TEMP_VALUE,
)

from .basebms import BaseBMS, BMSsample, crc_xmodem


class BMS(BaseBMS):
    """Dummy battery class implementation."""

    _HEAD: Final[int] = 0x7E
    _TAIL: Final[int] = 0x0D
    _CMD_VER: Final[int] = 0x10
    _RSP_VER: Final[int] = 0x14
    _MIN_LEN: Final[int] = 10
    _MAX_SUBS: Final[int] = 0xF
    _CELL_POS: Final[int] = 9
    _FIELDS: Final[  # Seplos V2: device manufacturer info 0x51, parallel data 0x62
        list[tuple[str, int, int, int, bool, Callable[[int], int | float]]]
    ] = [
        (KEY_PACK_COUNT, 0x51, 42, 1, False, lambda x: min(int(x), BMS._MAX_SUBS)),
        (KEY_PROBLEM, 0x62, 47, 6, False, lambda x: x),
    ]
    _PFIELDS: Final[  # Seplos V2: single machine data
        list[tuple[str, int, int, int, bool, Callable[[int], int | float]]]
    ] = [
        (ATTR_VOLTAGE, 0x61, 2, 2, False, lambda x: float(x / 100)),
        (ATTR_CURRENT, 0x61, 0, 2, True, lambda x: float(x / 100)),  # /10 for 0x62
        (ATTR_CYCLE_CHRG, 0x61, 4, 2, False, lambda x: float(x / 100)),  # /10 for 0x62
        (ATTR_CYCLES, 0x61, 13, 2, False, lambda x: x),
        (ATTR_BATTERY_LEVEL, 0x61, 9, 2, False, lambda x: float(x / 10)),
    ]
    _CMDS: Final[list[tuple[int, bytes]]] = [(0x51, b""), (0x61, b"\x00"), (0x62, b"")]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._exp_len: int = self._MIN_LEN

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": "BP0?",
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Seplos", "model": "Smart BMS V2"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("ff00")]

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "ff01"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "ff02"

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
        if (
            len(data) > BMS._MIN_LEN
            and data[0] == BMS._HEAD
            and len(self._data) >= self._exp_len
        ):
            self._exp_len = BMS._MIN_LEN + int.from_bytes(data[5:7])
            self._data = bytearray()

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        # verify that data is long enough
        if len(self._data) < self._exp_len:
            return

        if self._data[-1] != BMS._TAIL:
            self._log.debug("frame end incorrect: %s", self._data)
            return

        if self._data[1] != BMS._RSP_VER:
            self._log.debug("unknown frame version: V%.1f", self._data[1] / 10)
            return

        if self._data[4]:
            self._log.debug("BMS reported error code: 0x%X", self._data[4])
            return

        if (crc := crc_xmodem(self._data[1:-3])) != int.from_bytes(self._data[-3:-1]):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                int.from_bytes(self._data[-3:-1]),
                crc,
            )
            return

        self._log.debug(
            "address: 0x%X, function: 0x%X, return: 0x%X",
            self._data[2],
            self._data[3],
            self._data[4],
        )

        self._data_final[self._data[3]] = self._data
        self._data_event.set()

    async def _init_connection(self) -> None:
        """Initialize protocol state."""
        await super()._init_connection()
        self._exp_len = BMS._MIN_LEN

    @staticmethod
    def _cmd(cmd: int, address: int = 0, data: bytearray = bytearray()) -> bytearray:
        """Assemble a Seplos V2 BMS command."""
        assert cmd in (0x47, 0x51, 0x61, 0x62, 0x04)  # allow only read commands
        frame = bytearray(
            [BMS._HEAD, BMS._CMD_VER, address, 0x46, cmd]
        )  # fixed version
        frame += len(data).to_bytes(2, "big", signed=False) + data
        frame += bytearray(int.to_bytes(crc_xmodem(frame[1:]), 2, byteorder="big"))
        frame += bytearray([BMS._TAIL])
        return frame

    @staticmethod
    def _decode_data(data: dict[int, bytearray], offs: int) -> dict[str, int | float]:
        return {
            key: func(
                int.from_bytes(
                    data[cmd][idx + offs : idx + offs + size],
                    byteorder="big",
                    signed=sign,
                )
            )
            for key, cmd, idx, size, sign, func in BMS._PFIELDS
        }

    @staticmethod
    def _temp_sensors(data: bytearray, sensors: int, offs: int) -> dict[str, float]:
        return {
            f"{KEY_TEMP_VALUE}{idx}": (value - 2731.5) / 10
            for idx in range(sensors)
            if (
                value := int.from_bytes(
                    data[offs + idx * 2 : offs + (idx + 1) * 2],
                    byteorder="big",
                    signed=False,
                )
            )
        }

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": float(
                int.from_bytes(
                    data[10 + idx * 2 : 10 + idx * 2 + 2], byteorder="big", signed=False
                )
            )
            / 1000
            for idx in range(data[BMS._CELL_POS])
        }

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        for cmd, data in BMS._CMDS:
            await self._await_reply(BMS._cmd(cmd, data=bytearray(data)))

        result: BMSsample = {KEY_CELL_COUNT: int(self._data_final[0x61][BMS._CELL_POS])}
        result[KEY_TEMP_SENS] = int(
            self._data_final[0x61][BMS._CELL_POS + int(result[KEY_CELL_COUNT]) * 2 + 1]
        )

        result |= {
            key: func(
                int.from_bytes(
                    self._data_final[cmd][idx : idx + size],
                    byteorder="big",
                    signed=sign,
                )
            )
            for key, cmd, idx, size, sign, func in BMS._FIELDS
        }

        result |= BMS._cell_voltages(self._data_final[0x61])
        result |= BMS._temp_sensors(
            self._data_final[0x61],
            int(result[KEY_TEMP_SENS]),
            BMS._CELL_POS + int(result[KEY_CELL_COUNT]) * 2 + 2,
        )
        result |= BMS._decode_data(
            self._data_final,
            BMS._CELL_POS + int(result[KEY_CELL_COUNT] + result[KEY_TEMP_SENS]) * 2 + 2,
        )
        self._data_final.clear()

        return result
