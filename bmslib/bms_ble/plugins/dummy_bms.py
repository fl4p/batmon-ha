"""Module to support Dummy BMS."""

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSsample, BMSvalue


class BMS(BaseBMS):
    """Dummy BMS implementation."""

    # _HEAD: Final[bytes] = b"\x55"  # beginning of frame
    # _TAIL: Final[bytes] = b"\xAA"  # end of frame
    # _FRAME_LEN: Final[int] = 10  # length of frame, including SOF and checksum

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [{"local_name": "dummy", "connectable": True}]  # TODO

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Dummy Manufacturer", "model": "dummy model"}  # TODO

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("0000")]  # TODO: change service UUID here!

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "0000"  # TODO: change RX characteristic UUID here!

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "0000"  # TODO: change TX characteristic UUID here!

    @staticmethod
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {"power", "battery_charging"}
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        # self._log.debug("RX BLE data: %s", data)

        # *******************************************************
        # # TODO: Do things like checking correctness of frame here
        # # and store it into a instance variable, e.g. self._data
        # # Below are some examples of how to do it
        # # Have a look at the BMS base class for function to use,
        # # take a look at other implementations for more  details
        # *******************************************************

        # if not data.startswith(BMS._HEAD):
        #     self._log.debug("incorrect SOF")
        #     return

        # if (crc := crc_sum(self._data[:-1])) != self._data[-1]:
        #     self._log.debug("invalid checksum 0x%X != 0x%X", self._data[-1], crc)
        #     return

        # self._data = data.copy()
        # self._data_event.set()

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        self._log.debug("replace with command to UUID %s", BMS.uuid_tx())
        # await self._await_reply(b"<some_command>")

        # # TODO: parse data from self._data here

        return {
            "voltage": 12,
            "current": 1.5,
            "temperature": 27.182,
        }  # TODO: fixed values, replace parsed data
