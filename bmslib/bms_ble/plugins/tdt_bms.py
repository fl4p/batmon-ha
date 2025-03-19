"""Module to support TDT BMS."""

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
    """Dummy battery class implementation."""

    _UUID_CFG: Final[str] = "fffa"
    _HEAD: Final[int] = 0x7E
    _TAIL: Final[int] = 0x0D
    _CMD_VER: Final[int] = 0x00
    _RSP_VER: Final[int] = 0x00
    _CELL_POS: Final[int] = 0x8
    _INFO_LEN: Final[int] = 10  # minimal frame length
    _FIELDS: Final[
        list[tuple[str, int, int, int, bool, Callable[[int], int | float]]]
    ] = [
        (ATTR_VOLTAGE, 0x8C, 2, 2, False, lambda x: float(x / 100)),
        (
            ATTR_CURRENT,
            0x8C,
            0,
            2,
            False,
            lambda x: float((x & 0x3FFF) / 10 * (-1 if x >> 15 else 1)),
        ),
        (ATTR_CYCLE_CHRG, 0x8C, 4, 2, False, lambda x: float(x / 10)),
        (ATTR_BATTERY_LEVEL, 0x8C, 13, 1, False, lambda x: x),
        (ATTR_CYCLES, 0x8C, 8, 2, False, lambda x: x),
        (KEY_PROBLEM, 0x8D, 36, 2, False, lambda x: x),
    ]
    _CMDS: Final[list[int]] = [*list({field[1] for field in _FIELDS})]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._exp_len: int = BMS._INFO_LEN

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [{"manufacturer_id": 54976, "connectable": True}]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "TDT", "model": "Smart BMS"}

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
            ATTR_BATTERY_CHARGING,
            ATTR_CYCLE_CAP,
            ATTR_DELTA_VOLTAGE,
            ATTR_POWER,
            ATTR_RUNTIME,
            ATTR_TEMPERATURE,
        }  # calculate further values from BMS provided set ones

    async def _init_connection(self) -> None:
        await self._await_reply(
            data=b"HiLink", char=BMS._UUID_CFG, wait_for_notify=False
        )
        if (
            ret := int.from_bytes(await self._client.read_gatt_char(BMS._UUID_CFG))
        ) != 0x1:
            self._log.debug("error unlocking BMS: %X", ret)

        await super()._init_connection()
        self._exp_len = BMS._INFO_LEN

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        self._log.debug("RX BLE data: %s", data)

        if (
            len(data) > BMS._INFO_LEN
            and data[0] == BMS._HEAD
            and len(self._data) >= self._exp_len
        ):
            self._exp_len = BMS._INFO_LEN + int.from_bytes(data[6:8])
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

        if (crc := crc_modbus(self._data[:-3])) != int.from_bytes(
            self._data[-3:-1], "big"
        ):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                int.from_bytes(self._data[-3:-1], "big"),
                crc,
            )
            return
        self._data_final[self._data[5]] = self._data
        self._data_event.set()

    @staticmethod
    def _cmd(cmd: int, data: bytearray = bytearray()) -> bytearray:
        """Assemble a TDT BMS command."""
        assert cmd in (0x8C, 0x8D, 0x92)  # allow only read commands
        frame = bytearray(
            [BMS._HEAD, BMS._CMD_VER, 0x1, 0x3, 0x0, cmd]
        )  # fixed version
        frame += len(data).to_bytes(2, "big", signed=False) + data
        frame += bytearray(int.to_bytes(crc_modbus(frame), 2, byteorder="big"))
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
            for key, cmd, idx, size, sign, func in BMS._FIELDS
        }

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": float(
                int.from_bytes(
                    data[BMS._CELL_POS + 1 + idx * 2 : BMS._CELL_POS + 1 + idx * 2 + 2],
                    byteorder="big",
                    signed=False,
                )
            )
            / 1000
            for idx in range(data[BMS._CELL_POS])
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

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        for cmd in BMS._CMDS:
            await self._await_reply(BMS._cmd(cmd))

        result: BMSsample = {KEY_CELL_COUNT: int(self._data_final[0x8C][BMS._CELL_POS])}
        result[KEY_TEMP_SENS] = int(
            self._data_final[0x8C][BMS._CELL_POS + int(result[KEY_CELL_COUNT]) * 2 + 1]
        )

        result |= BMS._cell_voltages(self._data_final[0x8C])
        result |= BMS._temp_sensors(
            self._data_final[0x8C],
            int(result[KEY_TEMP_SENS]),
            BMS._CELL_POS + int(result[KEY_CELL_COUNT]) * 2 + 2,
        )
        idx: Final[int] = int(result[KEY_CELL_COUNT] + result[KEY_TEMP_SENS])
        result |= BMS._decode_data(
            self._data_final,
            BMS._CELL_POS + idx * 2 + 2,
        )
        result[KEY_PROBLEM] = int.from_bytes(
            self._data_final[0x8D][BMS._CELL_POS + idx + 6 : BMS._CELL_POS + idx + 8]
        )

        self._data_final.clear()

        return result
