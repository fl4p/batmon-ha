"""Module to support JBD Smart BMS."""

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
    """JBD Smart BMS class implementation."""

    HEAD_RSP: Final = bytes([0xDD])  # header for responses
    HEAD_CMD: Final = bytes([0xDD, 0xA5])  # read header for commands
    INFO_LEN: Final[int] = 7  # minimum frame size
    BASIC_INFO: Final[int] = 23  # basic info data length
    _FIELDS: Final[list[tuple[str, int, int, bool, Callable[[int], int | float]]]] = [
        (KEY_TEMP_SENS, 26, 1, False, lambda x: x),  # count is not limited
        (ATTR_VOLTAGE, 4, 2, False, lambda x: float(x / 100)),
        (ATTR_CURRENT, 6, 2, True, lambda x: float(x / 100)),
        (ATTR_BATTERY_LEVEL, 23, 1, False, lambda x: x),
        (ATTR_CYCLE_CHRG, 8, 2, False, lambda x: float(x / 100)),
        (ATTR_CYCLES, 12, 2, False, lambda x: x),
        (KEY_PROBLEM, 20, 2, False, lambda x: x),
    ]  # general protocol v4

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: bytearray = bytearray()

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
            for pattern in (
                "SP0?S*",
                "SP1?S*",
                "SP2?S*",
                "AP2?S*",
                "GJ-*",  # accurat batteries
                "SX1*",  # Supervolt v3
                "DP04S*",  # ECO-WORTHY, DCHOUSE
                "121?0*",  # Eleksol, Ultimatron
                "12200*",
                "12300*",
            )
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Jiabaida", "model": "Smart BMS"}

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
    def _calc_values() -> frozenset[str]:
        return frozenset(
            {
                ATTR_POWER,
                ATTR_BATTERY_CHARGING,
                ATTR_CYCLE_CAP,
                ATTR_RUNTIME,
                ATTR_DELTA_VOLTAGE,
                ATTR_TEMPERATURE,
            }
        )

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        # check if answer is a heading of basic info (0x3) or cell block info (0x4)
        if (
            data.startswith(self.HEAD_RSP)
            and len(self._data) > self.INFO_LEN
            and data[1] in (0x03, 0x04)
            and data[2] == 0x00
            and len(self._data) >= self.INFO_LEN + self._data[3]
        ):
            self._data = bytearray()

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        # verify that data is long enough
        if (
            len(self._data) < BMS.INFO_LEN
            or len(self._data) < BMS.INFO_LEN + self._data[3]
        ):
            return

        # check correct frame ending (0x77)
        frame_end: Final[int] = BMS.INFO_LEN + self._data[3] - 1
        if self._data[frame_end] != 0x77:
            self._log.debug("incorrect frame end (length: %i).", len(self._data))
            return

        if (crc := BMS._crc(self._data[2 : frame_end - 2])) != int.from_bytes(
            self._data[frame_end - 2 : frame_end], "big"
        ):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                int.from_bytes(self._data[frame_end - 2 : frame_end], "big"),
                crc,
            )
            return

        self._data_final = self._data
        self._data_event.set()

    @staticmethod
    def _crc(frame: bytes) -> int:
        """Calculate JBD frame CRC."""
        return 0x10000 - sum(frame)

    @staticmethod
    def _cmd(cmd: bytes) -> bytes:
        """Assemble a JBD BMS command."""
        frame = bytes([*BMS.HEAD_CMD, cmd[0], 0x00])
        frame += BMS._crc(frame[2:4]).to_bytes(2, "big")
        frame += bytes([0x77])
        return frame

    @staticmethod
    def _decode_data(data: bytearray) -> dict[str, int | float]:
        result: dict[str, int | float] = {
            key: func(
                int.from_bytes(data[idx : idx + size], byteorder="big", signed=sign)
            )
            for key, idx, size, sign, func in BMS._FIELDS
        }

        # calculate average temperature
        result |= {
            f"{KEY_TEMP_VALUE}{(idx-27)>>1}": (
                (int.from_bytes(data[idx : idx + 2], byteorder="big") - 2731) / 10
            )
            for idx in range(27, 27 + int(result[KEY_TEMP_SENS]) * 2, 2)
        }

        return result

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": float(
                int.from_bytes(
                    data[4 + idx * 2 : 4 + idx * 2 + 2], byteorder="big", signed=False
                )
            )
            / 1000
            for idx in range(int(data[3] / 2))
        }

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        data: BMSsample = {}
        for cmd, exp_len, dec_fct in (
            (BMS._cmd(b"\x03"), BMS.BASIC_INFO, BMS._decode_data),
            (BMS._cmd(b"\x04"), 0, BMS._cell_voltages),
        ):
            await self._await_reply(cmd)
            if (
                len(self._data_final) != BMS.INFO_LEN + self._data_final[3]
                or len(self._data_final) < BMS.INFO_LEN + exp_len
            ):
                self._log.debug(
                    "wrong data length (%i): %s",
                    len(self._data_final),
                    self._data_final,
                )

            data.update(dec_fct(self._data_final))

        return data
