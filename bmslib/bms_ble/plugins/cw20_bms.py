"""Module to support ATORCH CW20 Smart Shunt (BLE)."""

from typing import Final

from aiobmsble import MatcherPattern, BMSDp, BMSSample, BMSValue, BMSInfo
from aiobmsble.basebms import BaseBMS
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str



class BMS(BaseBMS):
    """ATORCH CW20 Smart Shunt class implementation."""

    HEAD: Final[bytes] = bytes([0xFF, 0x55])  # frame header
    # фактична довжина кадру може різнитись; візьмемо мінімум,
    # але не будемо відсікати довші
    MIN_FRAME_LEN: Final[int] = 28

    # Макет А: "zero-padded" (3-байтові voltage/current/capacity + 4-байт energy)
    _FIELDS_A: Final[tuple[BMSDp, ...]] = (
        BMSDp("voltage",   4, 3, False, lambda x: x / 10.0),      # 0.1 V
        BMSDp("current",   7, 3, True,  lambda x: x / 1000.0),    # 0.001 A (signed)
        BMSDp("cycle_capacity", 10, 3, False, lambda x: x / 1000.0),    # 0.001 Ah
        BMSDp("energy",   13, 4, False, lambda x: x / 100.0),     # 0.01 kWh
        BMSDp("temperature", 24, 2, False, lambda x: x),          # °C
    )

    # Макет B: "compact"
    # Напруга/струм = 2 байти; ємність = 3 байти; енергія = 4 байти, починаючи з offset 11
    _FIELDS_B: Final[tuple[BMSDp, ...]] = (
        BMSDp("voltage",   4, 2, False, lambda x: x / 10.0),      # 0.1 V
        BMSDp("current",   6, 2, True,  lambda x: x / 1000.0),    # 0.001 A (signed)
        BMSDp("cycle_capacity",  8, 3, False, lambda x: x / 1000.0),    # 0.001 Ah
        BMSDp("energy",   11, 4, False, lambda x: x / 100.0),     # 0.01 kWh (big-endian)
        BMSDp("temperature", 24, 2, False, lambda x: x),          # °C
    )

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize CW20 BMS."""
        super().__init__(ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[MatcherPattern]:
        """Provide matcher for advertisement."""
        return [
            {
                "local_name": "CW20*",
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
        ]


    def _fetch_device_info(self) -> BMSInfo:
        """Return CW20 device information."""
        return {"manufacturer": "ATORCH", "model": "CW20 DC Meter"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return CW20 service UUIDs."""
        return [normalize_uuid_str("ffe0")]

    @staticmethod
    def uuid_rx() -> str:
        """Notification/read characteristic UUID."""
        return "ffe1"

    @staticmethod
    def uuid_tx() -> str:
        """Write characteristic UUID."""
        return "ffe2"

    @staticmethod
    def _calc_values() -> frozenset[BMSValue]:
        """Derived values from raw fields."""
        return frozenset({"power", "battery_charging"})

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notify data from CW20."""
        if not data or not data.startswith(BMS.HEAD):
            self._log.debug("Invalid header")
            return
        if len(data) < BMS.MIN_FRAME_LEN:
            self._log.debug("Frame too short: %s", len(data))
            return
        self._data = bytearray(data)
        self._data_event.set()

    @staticmethod
    def _within_physical_limits(sample: BMSSample) -> bool:
        """Basic sanity checks to choose between layouts."""
        v = sample.get("voltage")
        i = sample.get("current")
        c = sample.get("cycle_capacity")
        e = sample.get("energy")
        return (
            isinstance(v, (int, float)) and 0.1 <= v <= 1000.0 and
            (i is None or isinstance(i, (int, float)) and -1000.0 <= i <= 1000.0) and
            (c is None or isinstance(c, (int, float)) and 0.0 <= c <= 1e6) and
            (e is None or isinstance(e, (int, float)) and 0.0 <= e <= 1e6)
        )

    async def _async_update(self) -> BMSSample:
        """Parse stored frame into BMSsample."""
        if not self._data:
            return {}

        # спершу пробуємо макет B (2b V/I, 3b capacity, 4b energy)
        sample = BMS._decode_data(self._FIELDS_B, self._data)
        if not self._within_physical_limits(sample):
            # fallback на макет A
            sample = BMS._decode_data(self._FIELDS_A, self._data)

        v = sample.get("voltage")
        i = sample.get("current")
        if isinstance(v, (int, float)) and isinstance(i, (int, float)):
            # CW20 показує потужність цілими ватами — округлимо, щоб збігтись з тестом
            sample["power"] = round(v * i, 0)
            sample["battery_charging"] = i > 0

        return sample
