"""Module to support CBT Power VB series BMS."""

from string import hexdigits
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
    lrc_modbus,
)


class BMS(BaseBMS):
    """CBT Power VB series battery class implementation."""

    _HEAD: Final[bytes] = b"\x7e"
    _TAIL: Final[bytes] = b"\x0d"
    _CMD_VER: Final[int] = 0x11  # TX protocol version
    _RSP_VER: Final[int] = 0x22  # RX protocol version
    _LEN_POS: Final[int] = 9
    _MIN_LEN: Final[int] = _LEN_POS + 3 + len(_HEAD) + len(_TAIL) + 4
    _MAX_LEN: Final[int] = 255
    _CELL_POS: Final[int] = 6

    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("voltage", 2, 2, False, lambda x: x / 10),
        BMSdp("current", 0, 2, True, lambda x: x / 10),
        BMSdp("battery_level", 4, 2, False, lambda x: min(x, 100)),
        BMSdp("cycles", 7, 2, False),
        BMSdp("problem_code", 15, 6, False, lambda x: x & 0xFFF000FF000F),
    )

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)
        self._exp_len: int = 0

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {  # Creabest
                "service_uuid": normalize_uuid_str("fff0"),
                "manufacturer_id": 16963,
                "connectable": True,
            },
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Creabest", "model": "VB series"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [
            normalize_uuid_str("ffe0"),
            normalize_uuid_str("ffe5"),
        ]

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "ffe4"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "ffe9"

    @staticmethod
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "battery_charging",
                "delta_voltage",
                "temperature",
                "power",
                "runtime",
                "cycle_capacity",
                "cycle_charge",
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""

        if len(data) > BMS._LEN_POS + 4 and data.startswith(BMS._HEAD):
            self._data = bytearray()
            try:
                length: Final[int] = int(data[BMS._LEN_POS : BMS._LEN_POS + 4], 16)
                self._exp_len = length & 0xFFF
                if BMS.lencs(length) != length >> 12:
                    self._exp_len = 0
                    self._log.debug("incorrect length checksum.")
            except ValueError:
                self._exp_len = 0

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        if len(self._data) < self._exp_len + BMS._MIN_LEN:
            return

        if not self._data.endswith(BMS._TAIL):
            self._log.debug("incorrect EOF: %s", data)
            self._data.clear()
            return

        if not all(chr(c) in hexdigits for c in self._data[1:-1]):
            self._log.debug("incorrect frame encoding.")
            self._data.clear()
            return

        if (ver := bytes.fromhex(self._data[1:3].decode())) != BMS._RSP_VER.to_bytes():
            self._log.debug("unknown response frame version: 0x%X", int.from_bytes(ver))
            self._data.clear()
            return

        if (crc := lrc_modbus(self._data[1:-5])) != int(self._data[-5:-1], 16):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X", crc, int(self._data[-5:-1], 16)
            )
            self._data.clear()
            return

        self._data = bytearray(
            bytes.fromhex(self._data.strip(BMS._HEAD + BMS._TAIL).decode())
        )
        self._data_event.set()

    @staticmethod
    def lencs(length: int) -> int:
        """Calculate the length checksum."""
        return (sum((length >> (i * 4)) & 0xF for i in range(3)) ^ 0xF) + 1 & 0xF

    @staticmethod
    def _cmd(cmd: int, dev_id: int = 1, data: bytes = b"") -> bytes:
        """Assemble a Seplos VB series command."""
        assert len(data) <= 0xFFF
        cdat: Final[bytes] = data + int.to_bytes(dev_id)
        frame = bytearray([BMS._CMD_VER, dev_id, 0x46, cmd])
        frame.extend(
            int.to_bytes(len(cdat) * 2 + (BMS.lencs(len(cdat) * 2) << 12), 2, "big")
        )
        frame.extend(cdat)
        frame.extend(
            int.to_bytes(lrc_modbus(bytearray(frame.hex().upper().encode())), 2, "big")
        )
        return BMS._HEAD + frame.hex().upper().encode() + BMS._TAIL

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        await self._await_reply(BMS._cmd(0x42))
        result: BMSsample = {"cell_count": self._data[BMS._CELL_POS]}
        temp_pos: Final[int] = BMS._CELL_POS + result.get("cell_count", 0) * 2 + 1
        result["temp_sensors"] = self._data[temp_pos]
        result["cell_voltages"] = BMS._cell_voltages(
            self._data, cells=result.get("cell_count", 0), start=BMS._CELL_POS + 1
        )
        result["temp_values"] = BMS._temp_values(
            self._data,
            values=result.get("temp_sensors", 0),
            start=temp_pos + 1,
            divider=10,
        )

        result |= BMS._decode_data(
            BMS._FIELDS, self._data, offset=temp_pos + 2 * result["temp_sensors"] + 1
        )

        await self._await_reply(BMS._cmd(0x81, 1, b"\x01\x00"), max_size=20)
        result["design_capacity"] = (
            int.from_bytes(self._data[6:8], byteorder="big", signed=False) // 10
        )

        return result
