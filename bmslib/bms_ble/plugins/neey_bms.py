"""Module to support Neey Smart BMS."""

from collections.abc import Callable
from struct import unpack_from
from typing import Any, Final, Literal

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSsample, BMSvalue, crc_sum


class BMS(BaseBMS):
    """Neey Smart BMS class implementation."""

    _BT_MODULE_MSG: Final = bytes([0x41, 0x54, 0x0D, 0x0A])  # AT\r\n from BLE module
    _HEAD_RSP: Final = bytes([0x55, 0xAA, 0x11, 0x01])  # start, dev addr, read cmd
    _HEAD_CMD: Final = bytes(
        [0xAA, 0x55, 0x11, 0x01]
    )  # header for commands (endiness!)
    _TAIL: Final[int] = 0xFF  # end of message
    _TYPE_POS: Final[int] = 4  # frame type is right after the header
    _MIN_FRAME: Final[int] = 10  # header length
    _FIELDS: Final[list[tuple[BMSvalue, int, str, Callable[[int], Any]]]] = [
        ("voltage", 201, "<f", lambda x: round(x, 3)),
        ("delta_voltage", 209, "<f", lambda x: round(x, 3)),
        ("problem_code", 216, "B", lambda x: x if x in {1, 3, 7, 8, 9, 10, 11} else 0),
        ("balance_current", 217, "<f", lambda x: round(x, 3)),
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(ble_device, reconnect)
        self._data_final: bytearray = bytearray()
        self._bms_info: dict[str, str] = {}
        self._exp_len: int = BMS._MIN_FRAME
        self._valid_reply: int = 0x02

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": normalize_uuid_str("fee7"),
                "connectable": True,
            }
            for pattern in ("EK-*", "GW-*")
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Neey", "model": "Balancer"}

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
        return "ffe1"

    @staticmethod
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset({"temperature"})

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Retrieve BMS data update."""

        if (
            len(self._data) >= self._exp_len or not self._data.startswith(BMS._HEAD_RSP)
        ) and data.startswith(BMS._HEAD_RSP):
            self._data = bytearray()
            self._exp_len = max(
                int.from_bytes(data[6:8], byteorder="little", signed=False),
                BMS._MIN_FRAME,
            )

        self._data += data

        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        # verify that data is long enough
        if len(self._data) < self._exp_len:
            return

        if not self._data.startswith(BMS._HEAD_RSP):
            self._log.debug("incorrect frame start.")
            return

        # trim message in case oversized
        if len(self._data) > self._exp_len:
            self._log.debug("wrong data length (%i): %s", len(self._data), self._data)
            self._data = self._data[: self._exp_len]

        if self._data[-1] != BMS._TAIL:
            self._log.debug("incorrect frame end.")
            return

        # check that message type is expected
        if self._data[BMS._TYPE_POS] != self._valid_reply:
            self._log.debug(
                "unexpected message type 0x%X (length %i): %s",
                self._data[BMS._TYPE_POS],
                len(self._data),
                self._data,
            )
            return

        if (crc := crc_sum(self._data[:-2])) != self._data[-2]:
            self._log.debug("invalid checksum 0x%X != 0x%X", self._data[-2], crc)
            return

        self._data_final = self._data.copy()
        self._data_event.set()

    async def _init_connection(
        self, char_notify: BleakGATTCharacteristic | int | str | None = None
    ) -> None:
        """Initialize RX/TX characteristics and protocol state."""
        await super()._init_connection(char_notify)

        # query device info frame (0x03) and wait for BMS ready (0xC8)
        self._valid_reply = 0x01
        await self._await_reply(self._cmd(b"\x01"))
        self._bms_info = BMS._dec_devinfo(self._data_final or bytearray())
        self._log.debug("device information: %s", self._bms_info)

        self._valid_reply = 0x02  # cell information

    @staticmethod
    def _cmd(cmd: bytes, reg: int = 0, value: list[int] | None = None) -> bytes:
        """Assemble a Neey BMS command."""
        value = [] if value is None else value
        assert len(value) <= 11
        frame: bytearray = bytearray(  # 0x14 frame length
            [*BMS._HEAD_CMD, cmd[0], reg & 0xFF, 0x14, *value]
        ) + bytearray(11 - len(value))
        frame += bytes([crc_sum(frame), BMS._TAIL])
        return bytes(frame)

    @staticmethod
    def _dec_devinfo(data: bytearray) -> dict[str, str]:
        fields: Final[dict[str, int]] = {
            "hw_version": 24,
            "sw_version": 32,
        }
        return {
            key: data[idx : idx + 8].decode(errors="replace").strip("\x00")
            for key, idx in fields.items()
        }

    @staticmethod
    def _cell_voltages(
        data: bytearray,
        *,
        cells: int,
        start: int,
        size: int = 2,
        byteorder: Literal["little", "big"] = "big",
        divider: int = 1000,
    ) -> list[float]:
        """Parse cell voltages from message."""
        return [
            round(value, 3)
            for idx in range(cells)
            if (value := unpack_from("<f", data, start + idx * size)[0])
        ]

    @staticmethod
    def _temp_sensors(data: bytearray, sensors: int) -> list[int | float]:
        return [
            round(unpack_from("<f", data, 221 + idx * 4)[0], 2)
            for idx in range(sensors)
        ]

    @staticmethod
    def _conv_data(data: bytearray) -> BMSsample:
        """Return BMS data from status message."""
        result: BMSsample = {}
        for key, idx, fmt, func in BMS._FIELDS:
            result[key] = func(unpack_from(fmt, data, idx)[0])

        return result

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        if not self._data_event.is_set() or self._data_final[4] != 0x02:
            # request cell info (only if data is not constantly published)
            self._log.debug("requesting cell info")
            await self._await_reply(data=BMS._cmd(b"\x02"))

        data: BMSsample = self._conv_data(self._data_final)
        data["temp_values"] = BMS._temp_sensors(self._data_final, 2)

        data["cell_voltages"] = BMS._cell_voltages(
            self._data_final, cells=24, start=9, byteorder="little", size=4
        )

        return data
