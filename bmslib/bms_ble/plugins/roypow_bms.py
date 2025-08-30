"""Module to support RoyPow BMS."""

from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSdp, BMSsample, BMSvalue


class BMS(BaseBMS):
    """RoyPow BMS implementation."""

    _HEAD: Final[bytes] = b"\xea\xd1\x01"
    _TAIL: Final[int] = 0xF5
    _BT_MODULE_MSG: Final[bytes] = b"AT+STAT\r\n"  # AT cmd from BLE module
    _MIN_LEN: Final[int] = len(_HEAD) + 1
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("battery_level", 7, 1, False, lambda x: x, 0x4),
        BMSdp("voltage", 47, 2, False, lambda x: x / 100, 0x4),
        BMSdp(
            "current",
            6,
            3,
            False,
            lambda x: (x & 0xFFFF) * (-1 if (x >> 16) & 0x1 else 1) / 100,
            0x3,
        ),
        BMSdp("problem_code", 9, 3, False, lambda x: x, 0x3),
        BMSdp(
            "cycle_charge",
            24,
            4,
            False,
            lambda x: ((x & 0xFFFF0000) | (x & 0xFF00) >> 8 | (x & 0xFF) << 8) / 1000,
            0x4,
        ),
        BMSdp("runtime", 30, 2, False, lambda x: x * 60, 0x4),
        BMSdp("temp_sensors", 13, 1, False, lambda x: x, 0x3),
        BMSdp("cycles", 9, 2, False, lambda x: x, 0x4),
    )
    _CMDS: Final[set[int]] = set({field.idx for field in _FIELDS})

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._exp_len: int = 0

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "service_uuid": BMS.uuid_services()[0],
                "manufacturer_id": manufacturer_id,
                "connectable": True,
            }
            for manufacturer_id in (0x01A8, 0x0B31, 0x8AFB)
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "RoyPow", "model": "SmartBMS"}

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
        if not (data := data.removeprefix(BMS._BT_MODULE_MSG)):
            self._log.debug("filtering AT cmd")
            return

        if (
            data.startswith(BMS._HEAD)
            and not self._data.startswith(BMS._HEAD)
            and len(data) > len(BMS._HEAD)
        ):
            self._exp_len = data[len(BMS._HEAD)]
            self._data.clear()

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        if not self._data.startswith(BMS._HEAD):
            self._data.clear()
            return

        # verify that data is long enough
        if len(self._data) < BMS._MIN_LEN + self._exp_len:
            return

        end_idx: Final[int] = BMS._MIN_LEN + self._exp_len - 1
        if self._data[end_idx] != BMS._TAIL:
            self._log.debug("incorrect EOF: %s", self._data)
            self._data.clear()
            return

        if (crc := BMS._crc(self._data[len(BMS._HEAD) : end_idx - 1])) != self._data[
            end_idx - 1
        ]:
            self._log.debug(
                "invalid checksum 0x%X != 0x%X", self._data[end_idx - 1], crc
            )
            self._data.clear()
            return

        self._data_final[self._data[5]] = self._data.copy()
        self._data.clear()
        self._data_event.set()

    @staticmethod
    def _crc(frame: bytearray) -> int:
        """Calculate XOR of all frame bytes."""
        crc: int = 0
        for b in frame:
            crc ^= b
        return crc

    @staticmethod
    def _cmd(cmd: bytes) -> bytes:
        """Assemble a RoyPow BMS command."""
        data: Final[bytearray] = bytearray([len(cmd) + 2, *cmd])
        return bytes([*BMS._HEAD, *data, BMS._crc(data), BMS._TAIL])

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        self._data.clear()
        self._data_final.clear()
        for cmd in range(2, 5):
            await self._await_reply(BMS._cmd(bytes([0xFF, cmd])))

        result: BMSsample = BMS._decode_data(BMS._FIELDS, self._data_final)

        # remove remaining runtime if battery is charging
        if result.get("runtime") == 0xFFFF * 60:
            result.pop("runtime", None)

        result["cell_voltages"] = BMS._cell_voltages(
            self._data_final.get(0x2, bytearray()),
            cells=max(0, (len(self._data_final.get(0x2, bytearray())) - 11) // 2),
            start=9,
        )
        result["temp_values"] = BMS._temp_values(
            self._data_final.get(0x3, bytearray()),
            values=result.get("temp_sensors", 0),
            start=14,
            size=1,
            signed=False,
            offset=40,
        )

        return result
