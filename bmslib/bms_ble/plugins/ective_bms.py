"""Module to support Ective BMS."""

import asyncio
from string import hexdigits
from typing import Final, Literal

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSdp, BMSsample, BMSvalue


class BMS(BaseBMS):
    """Ective BMS implementation."""

    _HEAD_RSP: Final[tuple[bytes, ...]] = (b"\x5e", b"\x83")  # header for responses
    _MAX_CELLS: Final[int] = 16
    _INFO_LEN: Final[int] = 113
    _CRC_LEN: Final[int] = 4
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("voltage", 1, 8, False, lambda x: x / 1000),
        BMSdp("current", 9, 8, True, lambda x: x / 1000),
        BMSdp("battery_level", 29, 4, False, lambda x: x),
        BMSdp("cycle_charge", 17, 8, False, lambda x: x / 1000),
        BMSdp("cycles", 25, 4, False, lambda x: x),
        BMSdp("temperature", 33, 4, False, lambda x: round(x * 0.1 - 273.15, 1)),
        BMSdp("problem_code", 37, 2, False, lambda x: x),
    )

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)
        self._data_final: bytearray = bytearray()

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
                "manufacturer_id": m_id,
            }
            for m_id in (0, 0xFFFF)
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Ective", "model": "Smart BMS"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("ffe0")]

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "ffe4"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        raise NotImplementedError

    @staticmethod
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "battery_charging",
                "cycle_capacity",
                "cycle_charge",
                "delta_voltage",
                "power",
                "runtime",
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""

        if (
            start := next(
                (i for i, b in enumerate(data) if bytes([b]) in BMS._HEAD_RSP), -1
            )
        ) != -1:  # check for beginning of frame
            data = data[start:]
            self._data.clear()

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        if len(self._data) < BMS._INFO_LEN:
            return

        self._data = self._data[: BMS._INFO_LEN]  # cut off exceeding data

        if not (
            self._data.startswith(BMS._HEAD_RSP)
            and set(self._data.decode(errors="replace")[1:]).issubset(hexdigits)
        ):
            self._log.debug("incorrect frame coding: %s", self._data)
            self._data.clear()
            return

        if (crc := BMS._crc(self._data[1 : -BMS._CRC_LEN])) != int(
            self._data[-BMS._CRC_LEN :], 16
        ):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                int(self._data[-BMS._CRC_LEN :], 16),
                crc,
            )
            self._data.clear()
            return

        self._data_final = self._data.copy()
        self._data_event.set()

    @staticmethod
    def _crc(data: bytearray) -> int:
        return sum(int(data[idx : idx + 2], 16) for idx in range(0, len(data), 2))

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
        """Parse cell voltages from status message."""
        return [
            (value / divider)
            for idx in range(cells)
            if (
                value := BMS._conv_int(
                    data[start + idx * size : start + (idx + 1) * size]
                )
            )
        ]

    @staticmethod
    def _conv_int(data: bytearray, sign: bool = False) -> int:
        return int.from_bytes(
            bytes.fromhex(data.decode("ascii", errors="strict")),
            byteorder="little",
            signed=sign,
        )

    @staticmethod
    def _conv_data(data: bytearray) -> BMSsample:
        result: BMSsample = {}
        for field in BMS._FIELDS:
            result[field.key] = field.fct(
                BMS._conv_int(data[field.pos : field.pos + field.size], field.signed)
            )
        return result

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        await asyncio.wait_for(self._wait_event(), timeout=BMS.TIMEOUT)
        return self._conv_data(self._data_final) | {
            "cell_voltages": BMS._cell_voltages(
                self._data_final, cells=BMS._MAX_CELLS, start=45, size=4
            )
        }
