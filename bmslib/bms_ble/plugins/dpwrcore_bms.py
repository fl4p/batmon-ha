"""Module to support D-powercore Smart BMS."""

from enum import IntEnum
from string import hexdigits
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSdp, BMSsample, BMSvalue


class Cmd(IntEnum):
    """BMS operation codes."""

    UNLOCKACC = 0x32
    UNLOCKREJ = 0x33
    LEGINFO1 = 0x60
    LEGINFO2 = 0x61
    CELLVOLT = 0x62
    UNLOCK = 0x64
    UNLOCKED = 0x65
    GETINFO = 0xA0


class BMS(BaseBMS):
    """D-powercore Smart BMS class implementation."""

    _PAGE_LEN: Final[int] = 20
    _MAX_CELLS: Final[int] = 32
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("voltage", 6, 2, False, lambda x: x / 10, Cmd.LEGINFO1),
        BMSdp("current", 8, 2, True, lambda x: x, Cmd.LEGINFO1),
        BMSdp("battery_level", 14, 1, False, lambda x: x, Cmd.LEGINFO1),
        BMSdp("cycle_charge", 12, 2, False, lambda x: x / 1000, Cmd.LEGINFO1),
        BMSdp(
            "temperature",
            12,
            2,
            False,
            lambda x: round(x * 0.1 - 273.15, 1),
            Cmd.LEGINFO2,
        ),
        BMSdp(
            "cell_count", 6, 1, False, lambda x: min(x, BMS._MAX_CELLS), Cmd.CELLVOLT
        ),
        BMSdp("cycles", 8, 2, False, lambda x: x, Cmd.LEGINFO2),
        BMSdp("problem_code", 15, 1, False, lambda x: x & 0xFF, Cmd.LEGINFO1),
    )
    _CMDS: Final[set[Cmd]] = {Cmd(field.idx) for field in _FIELDS}

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(ble_device, reconnect)
        assert self._ble_device.name is not None  # required for unlock
        self._data_final: dict[int, bytearray] = {}

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
            for pattern in ("DXB-*", "TBA-*")
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "D-powercore", "model": "Smart BMS"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("fff0")]

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "fff4"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        return "fff3"

    @staticmethod
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "battery_charging",
                "cycle_capacity",
                "delta_voltage",
                "power",
                "runtime",
            }
        )

    async def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        self._log.debug("RX BLE data: %s", data)

        if len(data) != BMS._PAGE_LEN:
            self._log.debug("invalid page length (%i)", len(data))
            return

        # ignore ACK responses
        if data[0] & 0x80:
            self._log.debug("ignore acknowledge message")
            return

        # acknowledge received frame
        await self._await_reply(
            bytes([data[0] | 0x80]) + data[1:], wait_for_notify=False
        )

        size: Final[int] = data[0]
        page: Final[int] = data[1] >> 4
        maxpg: Final[int] = data[1] & 0xF

        if page == 1:
            self._data.clear()

        self._data += data[2 : size + 2]

        self._log.debug("(%s): %s", "start" if page == 1 else "cnt.", data)

        if page == maxpg:
            if (crc := BMS._crc(self._data[3:-4])) != int.from_bytes(
                self._data[-4:-2], byteorder="big"
            ):
                self._log.debug(
                    "incorrect checksum: 0x%X != 0x%X",
                    int.from_bytes(self._data[-4:-2], byteorder="big"),
                    crc,
                )
                self._data.clear()
                self._data_final = {}  # reset invalid data
                return

            self._data_final[self._data[3]] = self._data.copy()
            self._data_event.set()

    @staticmethod
    def _crc(data: bytearray) -> int:
        return sum(data) + 8

    @staticmethod
    def _cmd(cmd: Cmd, data: bytes) -> bytes:
        frame: bytearray = bytearray([cmd.value, 0x00, 0x00]) + data
        checksum: Final[int] = BMS._crc(frame)
        frame = (
            bytearray([0x3A, 0x03, 0x05])
            + frame
            + bytes([(checksum >> 8) & 0xFF, checksum & 0xFF, 0x0D, 0x0A])
        )
        frame = bytearray([len(frame) + 2, 0x11]) + frame
        frame += bytes(BMS._PAGE_LEN - len(frame))

        return bytes(frame)

    async def _init_connection(
        self, char_notify: BleakGATTCharacteristic | int | str | None = None
    ) -> None:
        """Connect to the BMS and setup notification if not connected."""
        await super()._init_connection()

        # unlock BMS if not TBA version
        if self.name.startswith("TBA-"):
            return

        if not all(c in hexdigits for c in self.name[-4:]):
            self._log.debug("unable to unlock BMS")
            return

        pwd = int(self.name[-4:], 16)
        await self._await_reply(
            BMS._cmd(
                Cmd.UNLOCK,
                bytes([(pwd >> 8) & 0xFF, pwd & 0xFF]),
            ),
            wait_for_notify=False,
        )

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        for request in BMS._CMDS:
            await self._await_reply(self._cmd(request, b""))

        if not BMS._CMDS.issubset(set(self._data_final.keys())):
            raise ValueError("incomplete response set")

        result: BMSsample = BMS._decode_data(BMS._FIELDS, self._data_final)
        result["cell_voltages"] = BMS._cell_voltages(
            self._data_final[Cmd.CELLVOLT],
            cells=result.get("cell_count", 0),
            start=7,
        )

        self._data_final.clear()
        return result
