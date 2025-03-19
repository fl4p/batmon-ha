"""Module to support D-powercore Smart BMS."""

from collections.abc import Callable
from enum import IntEnum
from string import hexdigits
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
)

from .basebms import BaseBMS, BMSsample


class Cmd(IntEnum):
    """BMS operation codes."""

    UNLOCKACC = 0x32
    UNLOCKREJ = 0x33
    LEGINFO1 = 0x60
    LEGINFO2 = 0x61
    CELLVOLT = 0x62
    UNLOCK = 0x64
    UNLOCKED = 0x65
    GETINFO = 0xA0


class BMS(BaseBMS):
    """D-powercore Smart BMS class implementation."""

    _PAGE_LEN: Final[int] = 20
    _MAX_CELLS: Final[int] = 32
    _FIELDS: Final[list[tuple[str, Cmd, int, int, Callable[[int], int | float]]]] = [
        (ATTR_VOLTAGE, Cmd.LEGINFO1, 6, 2, lambda x: float(x) / 10),
        (ATTR_CURRENT, Cmd.LEGINFO1, 8, 2, lambda x: x),
        (ATTR_BATTERY_LEVEL, Cmd.LEGINFO1, 14, 1, lambda x: x),
        (ATTR_CYCLE_CHRG, Cmd.LEGINFO1, 12, 2, lambda x: float(x) / 1000),
        (
            ATTR_TEMPERATURE,
            Cmd.LEGINFO2,
            12,
            2,
            lambda x: round(float(x) * 0.1 - 273.15, 1),
        ),
        (KEY_CELL_COUNT, Cmd.CELLVOLT, 6, 1, lambda x: min(x, BMS._MAX_CELLS)),
        (ATTR_CYCLES, Cmd.LEGINFO2, 8, 2, lambda x: x),
        (KEY_PROBLEM, Cmd.LEGINFO1, 15, 1, lambda x: x & 0xFF),
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(__name__, ble_device, reconnect)
        assert self._ble_device.name is not None  # required for unlock
        self._data_final: bytearray = bytearray()

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": "DXB-*",
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            },
            {
                "local_name": "TBA-*",
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            },
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "D-powercore", "model": "Smart BMS"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("fff0")]

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "fff4"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "fff3"

    @staticmethod
    def _calc_values() -> set[str]:
        return {
            ATTR_BATTERY_CHARGING,
            ATTR_CYCLE_CAP,
            ATTR_DELTA_VOLTAGE,
            ATTR_POWER,
            ATTR_RUNTIME,
        }

    async def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        self._log.debug("RX BLE data: %s", data)

        if len(data) != BMS._PAGE_LEN:
            self._log.debug("invalid page length (%i)", len(data))
            return

        # ignore ACK responses
        if data[0] & 0x80:
            self._log.debug("ignore acknowledge message")
            return

        # acknowledge received frame
        await self._await_reply(
            bytearray([data[0] | 0x80]) + data[1:], wait_for_notify=False
        )

        size: Final[int] = int(data[0])
        page: Final[int] = int(data[1] >> 4)
        maxpg: Final[int] = int(data[1] & 0xF)

        if page == 1:
            self._data = bytearray()

        self._data += data[2 : size + 2]

        self._log.debug("(%s): %s", "start" if page == 1 else "cnt.", data)

        if page == maxpg:
            if (crc := BMS._crc(self._data[3:-4])) != int.from_bytes(
                self._data[-4:-2], byteorder="big"
            ):
                self._log.debug(
                    "incorrect checksum: 0x%X != 0x%X",
                    int.from_bytes(self._data[-4:-2], byteorder="big"),
                    crc,
                )
                self._data = bytearray()
                self._data_final = bytearray()  # reset invalid data
                return

            self._data_final = self._data
            self._data_event.set()

    @staticmethod
    def _crc(data: bytes) -> int:
        return sum(data) + 8

    @staticmethod
    def _cmd_frame(cmd: Cmd, data: bytes) -> bytes:
        frame: bytes = bytes([cmd.value, 0x00, 0x00]) + data
        checksum: Final[int] = BMS._crc(frame)
        frame = (
            bytes([0x3A, 0x03, 0x05])
            + frame
            + bytes([(checksum >> 8) & 0xFF, checksum & 0xFF, 0x0D, 0x0A])
        )
        frame = bytes([len(frame) + 2, 0x11]) + frame
        frame += bytes(BMS._PAGE_LEN - len(frame))

        return frame

    async def _init_connection(self) -> None:
        """Connect to the BMS and setup notification if not connected."""
        await super()._init_connection()

        # unlock BMS if not TBA version
        if self.name.startswith("TBA-"):
            return

        if not all(c in hexdigits for c in self.name[-4:]):
            self._log.debug("unable to unlock BMS")
            return

        pwd = int(self.name[-4:], 16)
        await self._await_reply(
            BMS._cmd_frame(
                Cmd.UNLOCK,
                bytes([(pwd >> 8) & 0xFF, pwd & 0xFF]),
            ),
            wait_for_notify=False,
        )

    @staticmethod
    def _cell_voltages(data: bytearray, cells: int) -> dict[str, float]:
        """Return cell voltages from status message."""
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": int.from_bytes(
                data[7 + 2 * idx : 7 + 2 * idx + 2], byteorder="big"
            )
            / 1000
            for idx in range(cells)
        }

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        data: BMSsample = {}
        for request in [Cmd.LEGINFO1, Cmd.LEGINFO2, Cmd.CELLVOLT]:
            await self._await_reply(self._cmd_frame(request, b""))

            data |= {
                key: func(
                    int.from_bytes(
                        self._data[idx : idx + size], byteorder="big", signed=True
                    )
                )
                for key, cmd, idx, size, func in BMS._FIELDS
                if cmd == request
            }
            if request == Cmd.CELLVOLT and data.get(KEY_CELL_COUNT):
                data.update(
                    BMS._cell_voltages(self._data_final, int(data[KEY_CELL_COUNT]))
                )

        return data
