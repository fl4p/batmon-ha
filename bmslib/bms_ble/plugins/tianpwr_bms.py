"""Module to support TianPwr BMS."""

from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSdp, BMSsample, BMSvalue


class BMS(BaseBMS):
    """TianPwr BMS implementation."""

    _HEAD: Final[bytes] = b"\x55"
    _TAIL: Final[bytes] = b"\xaa"
    _RDCMD: Final[bytes] = b"\x04"
    _MAX_CELLS: Final[int] = 16
    _MAX_TEMP: Final[int] = 6
    _MIN_LEN: Final[int] = 4
    _DEF_LEN: Final[int] = 20
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("battery_level", 3, 2, False, lambda x: x, 0x83),
        BMSdp("voltage", 5, 2, False, lambda x: x / 100, 0x83),
        BMSdp("current", 13, 2, True, lambda x: x / 100, 0x83),
        BMSdp("problem_code", 11, 8, False, lambda x: x, 0x84),
        BMSdp("cell_count", 3, 1, False, lambda x: x, 0x84),
        BMSdp("temp_sensors", 4, 1, False, lambda x: x, 0x84),
        BMSdp("design_capacity", 5, 2, False, lambda x: x // 100, 0x84),
        BMSdp("cycle_charge", 7, 2, False, lambda x: x / 100, 0x84),
        BMSdp("cycles", 9, 2, False, lambda x: x, 0x84),
    )
    _CMDS: Final[set[int]] = set({field.idx for field in _FIELDS}) | set({0x87})

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [{"local_name": "TP_*", "connectable": True}]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "TianPwr", "model": "SmartBMS"}

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
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "battery_charging",
                "cycle_capacity",
                "delta_voltage",
                "power",
                "temperature",
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        self._log.debug("RX BLE data: %s", data)

        # verify that data is long enough
        if len(data) != BMS._DEF_LEN:
            self._log.debug("incorrect frame length")
            return

        if not data.startswith(BMS._HEAD):
            self._log.debug("incorrect SOF.")
            return

        if not data.endswith(BMS._TAIL):
            self._log.debug("incorrect EOF.")
            return

        self._data_final[data[2]] = data.copy()
        self._data_event.set()

    @staticmethod
    def _cmd(addr: int) -> bytes:
        """Assemble a TianPwr BMS command."""
        return BMS._HEAD + BMS._RDCMD + addr.to_bytes(1) + BMS._TAIL

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        self._data_final.clear()
        for cmd in BMS._CMDS:
            await self._await_reply(BMS._cmd(cmd))

        result: BMSsample = BMS._decode_data(BMS._FIELDS, self._data_final)

        for cmd in range(
            0x88, 0x89 + min(result.get("cell_count", 0), BMS._MAX_CELLS) // 8
        ):
            await self._await_reply(BMS._cmd(cmd))
            result["cell_voltages"] = result.setdefault(
                "cell_voltages", []
            ) + BMS._cell_voltages(
                self._data_final.get(cmd, bytearray()), cells=8, start=3
            )

        if {0x83, 0x87}.issubset(self._data_final):
            result["temp_values"] = [
                int.from_bytes(
                    self._data_final[0x83][idx : idx + 2], byteorder="big", signed=True
                )
                / 10
                for idx in (7, 11)  # take ambient and mosfet temperature
            ] + BMS._temp_values(
                self._data_final.get(0x87, bytearray()),
                values=min(BMS._MAX_TEMP, result.get("temp_sensors", 0)),
                start=3,
                divider=10,
            )

        return result
