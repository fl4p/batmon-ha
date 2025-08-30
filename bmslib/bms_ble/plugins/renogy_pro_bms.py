"""Module to support Renogy Pro BMS."""

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BMSdp
from .renogy_bms import BMS as RenogyBMS


class BMS(RenogyBMS):
    """Renogy Pro battery class implementation."""

    HEAD: bytes = b"\xff\x03"  # SOP, read fct (x03)
    FIELDS: tuple[BMSdp, ...] = (
        BMSdp("voltage", 5, 2, False, lambda x: x / 10),
        BMSdp("current", 3, 2, True, lambda x: x / 10),
        BMSdp("design_capacity", 11, 4, False, lambda x: x // 1000),
        BMSdp("cycle_charge", 7, 4, False, lambda x: x / 1000),
        BMSdp("cycles", 15, 2, False, lambda x: x),
    )

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(ble_device, reconnect)
        self._char_write_handle: int = -1

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": "RNGRBP*",
                "manufacturer_id": 0xE14C,
                "connectable": True,
            },
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Renogy", "model": "Bluetooth battery pro"}

    async def _init_connection(
        self, char_notify: BleakGATTCharacteristic | int | str | None = None
    ) -> None:
        """Initialize RX/TX characteristics and protocol state."""
        char_notify_handle: int = -1
        self._char_write_handle = -1
        assert char_notify is None, "char_notify not used for Renogy Pro BMS"

        for service in self._client.services:
            self._log.debug(
                "service %s (#%i): %s",
                service.uuid,
                service.handle,
                service.description,
            )
            for char in service.characteristics:
                self._log.debug(
                    "characteristic %s (#%i): %s",
                    char.uuid,
                    char.handle,
                    char.properties,
                )
                if (
                    service.uuid == BMS.uuid_services()[0]
                    and char.uuid == normalize_uuid_str(BMS.uuid_tx())
                    and any(
                        prop in char.properties
                        for prop in ("write", "write-without-response")
                    )
                ):
                    self._char_write_handle = char.handle
                if (
                    service.uuid == BMS.uuid_services()[1]
                    and char.uuid == normalize_uuid_str(BMS.uuid_rx())
                    and "notify" in char.properties
                ):
                    char_notify_handle = char.handle

        if char_notify_handle == -1 or self._char_write_handle == -1:
            self._log.debug("failed to detect characteristics.")
            await self._client.disconnect()
            raise ConnectionError(f"Failed to detect characteristics from {self.name}.")
        self._log.debug(
            "using characteristics handle #%i (notify), #%i (write).",
            char_notify_handle,
            self._char_write_handle,
        )

        await super()._init_connection(char_notify_handle)

    async def _await_reply(
        self,
        data: bytes,
        char: int | str | None = None,
        wait_for_notify: bool = True,
        max_size: int = 0,
    ) -> None:
        """Send data to the BMS and wait for valid reply notification."""

        await super()._await_reply(
            data, self._char_write_handle, wait_for_notify, max_size
        )
