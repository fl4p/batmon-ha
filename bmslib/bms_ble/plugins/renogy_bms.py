"""Module to support Renogy BMS."""

from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import (
    AdvertisementPattern,
    BaseBMS,
    BMSdp,
    BMSsample,
    BMSvalue,
    crc_modbus,
)


class BMS(BaseBMS):
    """Renogy battery class implementation."""

    HEAD: bytes = b"\x30\x03"  # SOP, read fct (x03)
    _CRC_POS: Final[int] = -2
    _TEMP_POS: Final[int] = 37
    _CELL_POS: Final[int] = 3
    FIELDS: tuple[BMSdp, ...] = (
        BMSdp("voltage", 5, 2, False, lambda x: x / 10),
        BMSdp("current", 3, 2, True, lambda x: x / 100),
        BMSdp("design_capacity", 11, 4, False, lambda x: x // 1000),
        BMSdp("cycle_charge", 7, 4, False, lambda x: x / 1000),
        BMSdp("cycles", 15, 2, False, lambda x: x),
    )

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "service_uuid": BMS.uuid_services()[0],
                "manufacturer_id": 0x9860,
                "connectable": True,
            },
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Renogy", "model": "Bluetooth battery"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("ffd0"), normalize_uuid_str("fff0")]

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "fff1"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "ffd1"

    @staticmethod
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "power",
                "battery_charging",
                "temperature",
                "cycle_capacity",
                "battery_level",
                "runtime",
                "delta_voltage",
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        self._log.debug("RX BLE data: %s", data)

        if not data.startswith(BMS.HEAD) or len(data) < 3:
            self._log.debug("incorrect SOF")
            return

        if data[2] + 5 != len(data):
            self._log.debug("incorrect frame length: %i != %i", len(data), data[2] + 5)
            return

        if (crc := crc_modbus(data[: BMS._CRC_POS])) != int.from_bytes(
            data[BMS._CRC_POS :], "little"
        ):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                crc,
                int.from_bytes(data[BMS._CRC_POS :], "little"),
            )
            return

        self._data = data.copy()
        self._data_event.set()

    @staticmethod
    def _read_int16(data: bytearray, pos: int, signed: bool = False) -> int:
        return int.from_bytes(data[pos : pos + 2], byteorder="big", signed=signed)

    @staticmethod
    def _cmd(addr: int, words: int) -> bytes:
        """Assemble a Renogy BMS command (MODBUS)."""
        frame: bytearray = (
            bytearray(BMS.HEAD)
            + int.to_bytes(addr, 2, byteorder="big")
            + int.to_bytes(words, 2, byteorder="big")
        )

        frame.extend(int.to_bytes(crc_modbus(frame), 2, byteorder="little"))
        return bytes(frame)

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        await self._await_reply(self._cmd(0x13B2, 0x7))
        result: BMSsample = BMS._decode_data(type(self).FIELDS, self._data)

        await self._await_reply(self._cmd(0x1388, 0x22))
        result["cell_count"] = BMS._read_int16(self._data, BMS._CELL_POS)
        result["cell_voltages"] = BMS._cell_voltages(
            self._data,
            cells=min(16, result.get("cell_count", 0)),
            start=BMS._CELL_POS + 2,
            byteorder="big",
            divider=10,
        )

        result["temp_sensors"] = BMS._read_int16(self._data, BMS._TEMP_POS)
        result["temp_values"] = BMS._temp_values(
            self._data,
            values=min(16, result.get("temp_sensors", 0)),
            start=BMS._TEMP_POS + 2,
            divider=10,
        )

        await self._await_reply(self._cmd(0x13EC, 0x7))
        result["problem_code"] = int.from_bytes(self._data[3:-2], byteorder="big") & (
            ~0xE
        )

        return result
