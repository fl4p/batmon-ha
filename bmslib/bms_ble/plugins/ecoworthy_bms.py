"""Module to support ECO-WORTHY BMS."""

import asyncio
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
    """ECO-WORTHY BMS implementation."""

    _HEAD: Final[tuple] = (b"\xa1", b"\xa2")
    _CELL_POS: Final[int] = 14
    _TEMP_POS: Final[int] = 80
    _FIELDS_V1: Final[tuple[BMSdp, ...]] = (
        BMSdp("battery_level", 16, 2, False, lambda x: x, 0xA1),
        BMSdp("voltage", 20, 2, False, lambda x: x / 100, 0xA1),
        BMSdp("current", 22, 2, True, lambda x: x / 100, 0xA1),
        BMSdp("problem_code", 51, 2, False, lambda x: x, 0xA1),
        BMSdp("design_capacity", 26, 2, False, lambda x: x // 100, 0xA1),
        BMSdp("cell_count", _CELL_POS, 2, False, lambda x: x, 0xA2),
        BMSdp("temp_sensors", _TEMP_POS, 2, False, lambda x: x, 0xA2),
        # ("cycles", 0xA1, 8, 2, False, lambda x: x),
    )
    _FIELDS_V2: Final[tuple[BMSdp, ...]] = tuple(
        BMSdp(
            *field[:-2],
            (lambda x: x / 10) if field.key == "current" else field.fct,
            field.idx,
        )
        for field in _FIELDS_V1
    )

    _CMDS: Final[set[int]] = set({field.idx for field in _FIELDS_V1})

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)
        self._mac_head: Final[tuple] = tuple(
            int(self._ble_device.address.replace(":", ""), 16).to_bytes(6) + head
            for head in BMS._HEAD
        )
        self._data_final: dict[int, bytearray] = {}

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            AdvertisementPattern(local_name="ECO-WORTHY 02_*", connectable=True)
        ] + [
            AdvertisementPattern(
                local_name=pattern,
                service_uuid=BMS.uuid_services()[0],
                connectable=True,
            )
            for pattern in ("DCHOUSE*", "ECO-WORTHY*")
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "ECO-WORTHY", "model": "BW02"}

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
        raise NotImplementedError

    @staticmethod
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "battery_charging",
                "cycle_charge",
                "cycle_capacity",
                "delta_voltage",
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

        if not data.startswith(BMS._HEAD + self._mac_head):
            self._log.debug("invalid frame type: '%s'", data[0:1].hex())
            return

        if (crc := crc_modbus(data[:-2])) != int.from_bytes(data[-2:], "little"):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                int.from_bytes(data[-2:], "little"),
                crc,
            )
            self._data = bytearray()
            return

        # copy final data without message type and adapt to protocol type
        shift: Final[bool] = data.startswith(self._mac_head)
        self._data_final[data[6 if shift else 0]] = (
            bytearray(2 if shift else 0) + data.copy()
        )
        if BMS._CMDS.issubset(self._data_final.keys()):
            self._data_event.set()

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        self._data_final.clear()
        self._data_event.clear()  # clear event to ensure new data is acquired
        await asyncio.wait_for(self._wait_event(), timeout=BMS.TIMEOUT)

        result: BMSsample = BMS._decode_data(
            (
                BMS._FIELDS_V1
                if self._data_final[0xA1].startswith(BMS._HEAD)
                else BMS._FIELDS_V2
            ),
            self._data_final,
        )

        result["cell_voltages"] = BMS._cell_voltages(
            self._data_final[0xA2],
            cells=result.get("cell_count", 0),
            start=BMS._CELL_POS + 2,
        )
        result["temp_values"] = BMS._temp_values(
            self._data_final[0xA2],
            values=result.get("temp_sensors", 0),
            start=BMS._TEMP_POS + 2,
            divider=10,
        )

        return result
