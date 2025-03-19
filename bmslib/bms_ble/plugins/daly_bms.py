"""Module to support Daly Smart BMS."""

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
    KEY_CELL_COUNT,
    KEY_CELL_VOLTAGE,
    KEY_PROBLEM,
    KEY_TEMP_SENS,
    KEY_TEMP_VALUE,
)

from .basebms import BaseBMS, BMSsample, crc_modbus


class BMS(BaseBMS):
    """Daly Smart BMS class implementation."""

    HEAD_READ: Final[bytes] = b"\xD2\x03"
    CMD_INFO: Final[bytes] = b"\x00\x00\x00\x3E\xD7\xB9"
    MOS_INFO: Final[bytes] = b"\x00\x3E\x00\x09\xF7\xA3"
    HEAD_LEN: Final[int] = 3
    CRC_LEN: Final[int] = 2
    MAX_CELLS: Final[int] = 32
    MAX_TEMP: Final[int] = 8
    INFO_LEN: Final[int] = 84 + HEAD_LEN + CRC_LEN + MAX_CELLS + MAX_TEMP
    MOS_TEMP_POS: Final[int] = HEAD_LEN + 8
    _FIELDS: Final[list[tuple[str, int, int, Callable[[int], int | float]]]] = [
        (ATTR_VOLTAGE, 80 + HEAD_LEN, 2, lambda x: float(x / 10)),
        (ATTR_CURRENT, 82 + HEAD_LEN, 2, lambda x: float((x - 30000) / 10)),
        (ATTR_BATTERY_LEVEL, 84 + HEAD_LEN, 2, lambda x: float(x / 10)),
        (ATTR_CYCLE_CHRG, 96 + HEAD_LEN, 2, lambda x: float(x / 10)),
        (KEY_CELL_COUNT, 98 + HEAD_LEN, 2, lambda x: min(x, BMS.MAX_CELLS)),
        (KEY_TEMP_SENS, 100 + HEAD_LEN, 2, lambda x: min(x, BMS.MAX_TEMP)),
        (ATTR_CYCLES, 102 + HEAD_LEN, 2, lambda x: x),
        (ATTR_DELTA_VOLTAGE, 112 + HEAD_LEN, 2, lambda x: float(x / 1000)),
        (KEY_PROBLEM, 116 + HEAD_LEN, 8, lambda x: x % 2**64),
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(__name__, ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": "DL-*",
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            },
        ] + [
            {"manufacturer_id": m_id, "connectable": True}
            for m_id in (0x102, 0x104, 0x0302)
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Daly", "model": "Smart BMS"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("fff0")]

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "fff1"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "fff2"

    @staticmethod
    def _calc_values() -> set[str]:
        return {
            ATTR_CYCLE_CAP,
            ATTR_POWER,
            ATTR_BATTERY_CHARGING,
            ATTR_RUNTIME,
            ATTR_TEMPERATURE,
        }

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        self._log.debug("RX BLE data: %s", data)

        if (
            len(data) < BMS.HEAD_LEN
            or data[0:2] != BMS.HEAD_READ
            or int(data[2]) + 1 != len(data) - len(BMS.HEAD_READ) - BMS.CRC_LEN
        ):
            self._log.debug("response data is invalid")
            return

        if (crc := crc_modbus(data[:-2])) != int.from_bytes(
            data[-2:], byteorder="little"
        ):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                int.from_bytes(data[-2:], byteorder="little"),
                crc,
            )
            self._data.clear()
            return

        self._data = data
        self._data_event.set()

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        data: BMSsample = {}
        try:
            # request MOS temperature (possible outcome: response, empty response, no response)
            await self._await_reply(BMS.HEAD_READ + BMS.MOS_INFO)

            if sum(self._data[BMS.MOS_TEMP_POS :][:2]):
                self._log.debug("MOS info: %s", self._data)
                data |= {
                    f"{KEY_TEMP_VALUE}0": float(
                        int.from_bytes(
                            self._data[BMS.MOS_TEMP_POS :][:2],
                            byteorder="big",
                            signed=True,
                        )
                        - 40
                    )
                }
        except TimeoutError:
            self._log.debug("no MOS temperature available.")

        await self._await_reply(BMS.HEAD_READ + BMS.CMD_INFO)

        if len(self._data) != BMS.INFO_LEN:
            self._log.debug("incorrect frame length: %i", len(self._data))
            return {}

        data |= {
            key: func(
                int.from_bytes(
                    self._data[idx : idx + size], byteorder="big", signed=True
                )
            )
            for key, idx, size, func in BMS._FIELDS
        }

        # get temperatures
        # shift index if MOS temperature is available
        t_off: Final[int] = 1 if f"{KEY_TEMP_VALUE}0" in data else 0
        data |= {
            f"{KEY_TEMP_VALUE}{((idx-64-BMS.HEAD_LEN)>>1) + t_off}": float(
                int.from_bytes(self._data[idx : idx + 2], byteorder="big", signed=True)
                - 40
            )
            for idx in range(
                64 + self.HEAD_LEN, 64 + self.HEAD_LEN + int(data[KEY_TEMP_SENS]) * 2, 2
            )
        }

        # get cell voltages
        data |= {
            f"{KEY_CELL_VOLTAGE}{idx}": float(
                int.from_bytes(
                    self._data[BMS.HEAD_LEN + 2 * idx : BMS.HEAD_LEN + 2 * idx + 2],
                    byteorder="big",
                    signed=True,
                )
                / 1000
            )
            for idx in range(int(data[KEY_CELL_COUNT]))
        }

        return data
