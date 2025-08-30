"""Module to support Seplos v2 BMS."""

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
    crc_xmodem,
)


class BMS(BaseBMS):
    """Seplos v2 BMS implementation."""

    _HEAD: Final[bytes] = b"\x7e"
    _TAIL: Final[bytes] = b"\x0d"
    _CMD_VER: Final[int] = 0x10  # TX protocol version
    _RSP_VER: Final[int] = 0x14  # RX protocol version
    _MIN_LEN: Final[int] = 10
    _MAX_SUBS: Final[int] = 0xF
    _CELL_POS: Final[int] = 9
    _PRB_MAX: Final[int] = 8  # max number of alarm event bytes
    _PRB_MASK: Final[int] = ~0x82FFFF  # ignore byte 7-8 + byte 6 (bit 7,2)
    _PFIELDS: Final[tuple[BMSdp, ...]] = (  # Seplos V2: single machine data
        BMSdp("voltage", 2, 2, False, lambda x: x / 100),
        BMSdp("current", 0, 2, True, lambda x: x / 100),  # /10 for 0x62
        BMSdp("cycle_charge", 4, 2, False, lambda x: x / 100),  # /10 for 0x62
        BMSdp("cycles", 13, 2, False, lambda x: x),
        BMSdp("battery_level", 9, 2, False, lambda x: x / 10),
    )
    _GSMD_LEN: Final[int] = _CELL_POS + max((dp.pos + dp.size) for dp in _PFIELDS) + 3
    _CMDS: Final[list[tuple[int, bytes]]] = [(0x51, b""), (0x61, b"\x00"), (0x62, b"")]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._exp_len: int = BMS._MIN_LEN
        self._exp_reply: set[int] = set()

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
            for pattern in ("BP0?", "BP1?", "BP2?")
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Seplos", "model": "Smart BMS V2"}

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
                "runtime",
                "temperature",
            }
        )  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""
        if (
            len(data) > BMS._MIN_LEN
            and data.startswith(BMS._HEAD)
            and len(self._data) >= self._exp_len
        ):
            self._exp_len = BMS._MIN_LEN + int.from_bytes(data[5:7])
            self._data = bytearray()

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        # verify that data is long enough
        if len(self._data) < self._exp_len:
            return

        if not self._data.endswith(BMS._TAIL):
            self._log.debug("incorrect frame end: %s", self._data)
            return

        if self._data[1] != BMS._RSP_VER:
            self._log.debug("unknown frame version: V%.1f", self._data[1] / 10)
            return

        if self._data[4]:
            self._log.debug("BMS reported error code: 0x%X", self._data[4])
            return

        if (crc := crc_xmodem(self._data[1:-3])) != int.from_bytes(self._data[-3:-1]):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                crc,
                int.from_bytes(self._data[-3:-1]),
            )
            return

        self._log.debug(
            "address: 0x%X, function: 0x%X, return: 0x%X",
            self._data[2],
            self._data[3],
            self._data[4],
        )

        self._data_final[self._data[3]] = self._data
        try:
            self._exp_reply.remove(self._data[3])
            self._data_event.set()
        except KeyError:
            self._log.debug("unexpected reply: 0x%X", self._data[3])

    async def _init_connection(
        self, char_notify: BleakGATTCharacteristic | int | str | None = None
    ) -> None:
        """Initialize protocol state."""
        await super()._init_connection()
        self._exp_len = BMS._MIN_LEN

    @staticmethod
    def _cmd(cmd: int, address: int = 0, data: bytearray = bytearray()) -> bytes:
        """Assemble a Seplos V2 BMS command."""
        assert cmd in (0x47, 0x51, 0x61, 0x62, 0x04)  # allow only read commands
        frame = bytearray([*BMS._HEAD, BMS._CMD_VER, address, 0x46, cmd])
        frame += len(data).to_bytes(2, "big", signed=False) + data
        frame += int.to_bytes(crc_xmodem(frame[1:]), 2, byteorder="big") + BMS._TAIL
        return bytes(frame)

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        for cmd, data in BMS._CMDS:
            self._exp_reply.add(cmd)
            await self._await_reply(BMS._cmd(cmd, data=bytearray(data)))

        result: BMSsample = {}
        result["cell_count"] = self._data_final[0x61][BMS._CELL_POS]
        result["temp_sensors"] = self._data_final[0x61][
            BMS._CELL_POS + result["cell_count"] * 2 + 1
        ]
        ct_blk_len: Final[int] = (result["cell_count"] + result["temp_sensors"]) * 2 + 2

        if (BMS._GSMD_LEN + ct_blk_len) > len(self._data_final[0x61]):
            raise ValueError("message too short to decode data")

        result |= BMS._decode_data(
            BMS._PFIELDS, self._data_final[0x61], offset=BMS._CELL_POS + ct_blk_len
        )

        # get extention pack count from parallel data (main pack)
        result["pack_count"] = self._data_final[0x51][42]

        # get alarms from parallel data (main pack)
        alarm_evt: Final[int] = min(self._data_final[0x62][46], BMS._PRB_MAX)
        result["problem_code"] = (
            int.from_bytes(self._data_final[0x62][47 : 47 + alarm_evt], byteorder="big")
            & BMS._PRB_MASK
        )

        result["cell_voltages"] = BMS._cell_voltages(
            self._data_final[0x61],
            cells=self._data_final[0x61][BMS._CELL_POS],
            start=10,
        )
        result["temp_values"] = BMS._temp_values(
            self._data_final[0x61],
            values=result["temp_sensors"],
            start=BMS._CELL_POS + result.get("cell_count", 0) * 2 + 2,
            signed=False,
            offset=2731,
            divider=10,
        )

        self._data_final.clear()

        return result
