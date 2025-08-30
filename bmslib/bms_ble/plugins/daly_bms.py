"""Module to support Daly Smart BMS."""

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
    """Daly Smart BMS class implementation."""

    HEAD_READ: Final[bytes] = b"\xd2\x03"
    CMD_INFO: Final[bytes] = b"\x00\x00\x00\x3e\xd7\xb9"
    MOS_INFO: Final[bytes] = b"\x00\x3e\x00\x09\xf7\xa3"
    HEAD_LEN: Final[int] = 3
    CRC_LEN: Final[int] = 2
    MAX_CELLS: Final[int] = 32
    MAX_TEMP: Final[int] = 8
    INFO_LEN: Final[int] = 84 + HEAD_LEN + CRC_LEN + MAX_CELLS + MAX_TEMP
    MOS_TEMP_POS: Final[int] = HEAD_LEN + 8
    MOS_NOT_AVAILABLE: Final[tuple[str]] = ("DL-FB4C2E0",)
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("voltage", 80, 2, False, lambda x: x / 10),
        BMSdp("current", 82, 2, False, lambda x: (x - 30000) / 10),
        BMSdp("battery_level", 84, 2, False, lambda x: x / 10),
        BMSdp("cycle_charge", 96, 2, False, lambda x: x / 10),
        BMSdp("cell_count", 98, 2, False, lambda x: min(x, BMS.MAX_CELLS)),
        BMSdp("temp_sensors", 100, 2, False, lambda x: min(x, BMS.MAX_TEMP)),
        BMSdp("cycles", 102, 2, False, lambda x: x),
        BMSdp("delta_voltage", 112, 2, False, lambda x: x / 1000),
        BMSdp("problem_code", 116, 8, False, lambda x: x % 2**64),
    )

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            AdvertisementPattern(
                local_name="DL-*",
                service_uuid=BMS.uuid_services()[0],
                connectable=True,
            )
        ] + [
            AdvertisementPattern(
                manufacturer_id=m_id,
                connectable=True,
            )
            for m_id in (0x102, 0x104, 0x0302, 0x0303)
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
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "cycle_capacity",
                "power",
                "battery_charging",
                "runtime",
                "temperature",
            }
        )

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        self._log.debug("RX BLE data: %s", data)

        if (
            len(data) < BMS.HEAD_LEN
            or data[0:2] != BMS.HEAD_READ
            or data[2] + 1 != len(data) - len(BMS.HEAD_READ) - BMS.CRC_LEN
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
        result: BMSsample = {}
        if (  # do not query devices that do not support MOS temperature, e.g. Bulltron
            not self.name or not self.name.startswith(BMS.MOS_NOT_AVAILABLE)
        ):
            try:
                # request MOS temperature (possible outcome: response, empty response, no response)
                await self._await_reply(BMS.HEAD_READ + BMS.MOS_INFO)

                if sum(self._data[BMS.MOS_TEMP_POS :][:2]):
                    self._log.debug("MOS info: %s", self._data)
                    result["temp_values"] = [
                        int.from_bytes(
                            self._data[BMS.MOS_TEMP_POS :][:2],
                            byteorder="big",
                            signed=True,
                        )
                        - 40
                    ]
            except TimeoutError:
                self._log.debug("no MOS temperature available.")

        await self._await_reply(BMS.HEAD_READ + BMS.CMD_INFO)

        if len(self._data) != BMS.INFO_LEN:
            self._log.debug("incorrect frame length: %i", len(self._data))
            return {}

        result |= BMS._decode_data(BMS._FIELDS, self._data, offset=BMS.HEAD_LEN)

        # add temperature sensors
        result.setdefault("temp_values", []).extend(
            BMS._temp_values(
                self._data,
                values=result.get("temp_sensors", 0),
                start=64 + BMS.HEAD_LEN,
                offset=40,
            )
        )

        # get cell voltages
        result["cell_voltages"] = BMS._cell_voltages(
            self._data, cells=result.get("cell_count", 0), start=BMS.HEAD_LEN
        )

        return result
