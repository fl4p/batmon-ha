"""Module to support Redodo BMS."""

from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSdp, BMSsample, BMSvalue, crc_sum


class BMS(BaseBMS):
    """Redodo BMS implementation."""

    _HEAD_LEN: Final[int] = 3
    _MAX_CELLS: Final[int] = 16
    _MAX_TEMP: Final[int] = 3
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("voltage", 12, 2, False, lambda x: x / 1000),
        BMSdp("current", 48, 4, True, lambda x: x / 1000),
        BMSdp("battery_level", 90, 2, False, lambda x: x),
        BMSdp("cycle_charge", 62, 2, False, lambda x: x / 100),
        BMSdp("cycles", 96, 4, False, lambda x: x),
        BMSdp("problem_code", 76, 4, False, lambda x: x),
    )

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {  # patterns required to exclude "BT-ROCC2440"
                "local_name": pattern,
                "service_uuid": BMS.uuid_services()[0],
                "manufacturer_id": 0x585A,
                "connectable": True,
            }
            for pattern in (
                "R-12*",
                "R-24*",
                "RO-12*",
                "RO-24*",
                "P-12*",
                "P-24*",
                "PQ-12*",
                "PQ-24*",
                "L-12*",  # vv *** LiTime *** vv
                "L-24*",
                "L-51*",
                "LT-12???BG-A0[7-9]*",  # LiTime based on ser#
                "LT-51*",
            )
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Redodo", "model": "Bluetooth battery"}

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
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "battery_charging",
                "delta_voltage",
                "cycle_capacity",
                "power",
                "runtime",
                "temperature",
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        self._log.debug("RX BLE data: %s", data)

        if len(data) < 3 or not data.startswith(b"\x00\x00"):
            self._log.debug("incorrect SOF.")
            return

        if len(data) != data[2] + BMS._HEAD_LEN + 1:  # add header length and CRC
            self._log.debug("incorrect frame length (%i)", len(data))
            return

        if (crc := crc_sum(data[:-1])) != data[-1]:
            self._log.debug("invalid checksum 0x%X != 0x%X", data[len(data) - 1], crc)
            return

        self._data = data
        self._data_event.set()

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        await self._await_reply(b"\x00\x00\x04\x01\x13\x55\xaa\x17")

        result: BMSsample = BMS._decode_data(
            BMS._FIELDS, self._data, byteorder="little"
        )
        result["cell_voltages"] = BMS._cell_voltages(
            self._data, cells=BMS._MAX_CELLS, start=16, byteorder="little"
        )
        result["temp_values"] = BMS._temp_values(
            self._data, values=BMS._MAX_TEMP, start=52, byteorder="little"
        )

        return result
