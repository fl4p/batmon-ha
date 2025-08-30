"""Module to support Seplos V3 Smart BMS."""

from collections.abc import Callable
from typing import Any, Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import (
    AdvertisementPattern,
    BaseBMS,
    BMSdp,
    BMSpackvalue,
    BMSsample,
    BMSvalue,
    crc_modbus,
)


class BMS(BaseBMS):
    """Seplos V3 Smart BMS class implementation."""

    CMD_READ: Final[list[int]] = [0x01, 0x04]
    HEAD_LEN: Final[int] = 3
    CRC_LEN: Final[int] = 2
    PIA_LEN: Final[int] = 0x11
    PIB_LEN: Final[int] = 0x1A
    EIA_LEN: Final[int] = PIB_LEN
    EIB_LEN: Final[int] = 0x16
    EIC_LEN: Final[int] = 0x5
    _TEMP_START: Final[int] = HEAD_LEN + 32
    QUERY: Final[dict[str, tuple[int, int, int]]] = {
        # name: cmd, reg start, length
        "EIA": (0x4, 0x2000, EIA_LEN),
        "EIB": (0x4, 0x2100, EIB_LEN),
        "EIC": (0x1, 0x2200, EIC_LEN),
    }
    PQUERY: Final[dict[str, tuple[int, int, int]]] = {
        "PIA": (0x4, 0x1000, PIA_LEN),
        "PIB": (0x4, 0x1100, PIB_LEN),
    }
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("temperature", 20, 2, True, lambda x: x / 10, EIB_LEN),  # avg. ctemp
        BMSdp("voltage", 0, 4, False, lambda x: BMS._swap32(x) / 100, EIA_LEN),
        BMSdp("current", 4, 4, True, lambda x: BMS._swap32(x, True) / 10, EIA_LEN),
        BMSdp("cycle_charge", 8, 4, False, lambda x: BMS._swap32(x) / 100, EIA_LEN),
        BMSdp("pack_count", 44, 2, False, lambda x: x, EIA_LEN),
        BMSdp("cycles", 46, 2, False, lambda x: x, EIA_LEN),
        BMSdp("battery_level", 48, 2, False, lambda x: x / 10, EIA_LEN),
        BMSdp("problem_code", 1, 9, False, lambda x: x & 0xFFFF00FF00FF0000FF, EIC_LEN),
    )  # Protocol Seplos V3
    _PFIELDS: Final[list[tuple[BMSpackvalue, int, bool, Callable[[int], Any]]]] = [
        ("pack_voltages", 0, False, lambda x: x / 100),
        ("pack_currents", 2, True, lambda x: x / 100),
        ("pack_battery_levels", 10, False, lambda x: x / 10),
        ("pack_cycles", 14, False, lambda x: x),
    ]  # Protocol Seplos V3
    _CMDS: Final[set[int]] = {field[2] for field in QUERY.values()} | {
        field[2] for field in PQUERY.values()
    }

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._pack_count: int = 0  # number of battery packs
        self._pkglen: int = 0  # expected packet length

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
            for pattern in {f"SP{num}?B*" for num in range(10)} | {"CSY*"}
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Seplos", "model": "Smart BMS V3"}

    # setup UUIDs
    #    serv 0000fff0-0000-1000-8000-00805f9b34fb
    # 	 char 0000fff1-0000-1000-8000-00805f9b34fb (#16): ['read', 'notify']
    # 	 char 0000fff2-0000-1000-8000-00805f9b34fb (#20): ['read', 'write-without-response', 'write']
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
        return frozenset({"power", "battery_charging", "cycle_capacity", "runtime"})

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Retrieve BMS data update."""

        if (
            len(data) > BMS.HEAD_LEN + BMS.CRC_LEN
            and data[0] <= self._pack_count
            and data[1] & 0x7F in BMS.CMD_READ  # include read errors
            and data[2] >= BMS.HEAD_LEN + BMS.CRC_LEN
        ):
            self._data = bytearray()
            self._pkglen = data[2] + BMS.HEAD_LEN + BMS.CRC_LEN
        elif (  # error message
            len(data) == BMS.HEAD_LEN + BMS.CRC_LEN
            and data[0] <= self._pack_count
            and data[1] & 0x80
        ):
            self._log.debug("RX error: %X", data[2])
            self._data = bytearray()
            self._pkglen = BMS.HEAD_LEN + BMS.CRC_LEN

        self._data += data
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )

        # verify that data is long enough
        if len(self._data) < self._pkglen:
            return

        if (crc := crc_modbus(self._data[: self._pkglen - 2])) != int.from_bytes(
            self._data[self._pkglen - 2 : self._pkglen], "little"
        ):
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                int.from_bytes(self._data[self._pkglen - 2 : self._pkglen], "little"),
                crc,
            )
            self._data = bytearray()
            return

        if self._data[2] >> 1 not in BMS._CMDS or self._data[1] & 0x80:
            self._log.debug(
                "unknown message: %s, length: %s", self._data[0:2], self._data[2]
            )
            self._data = bytearray()
            return

        if len(self._data) != self._pkglen:
            self._log.debug(
                "wrong data length (%i!=%s): %s",
                len(self._data),
                self._pkglen,
                self._data,
            )

        self._data_final[self._data[0] << 8 | self._data[2] >> 1] = self._data
        self._data = bytearray()
        self._data_event.set()

    async def _init_connection(
        self, char_notify: BleakGATTCharacteristic | int | str | None = None
    ) -> None:
        """Initialize RX/TX characteristics."""
        await super()._init_connection()
        self._pack_count = 0
        self._pkglen = 0

    @staticmethod
    def _swap32(value: int, signed: bool = False) -> int:
        """Swap high and low 16bit in 32bit integer."""

        value = ((value >> 16) & 0xFFFF) | (value & 0xFFFF) << 16
        if signed and value & 0x80000000:
            value = -0x100000000 + value
        return value

    @staticmethod
    def _cmd(device: int, cmd: int, start: int, count: int) -> bytes:
        """Assemble a Seplos BMS command."""
        assert device >= 0x00 and (device <= 0x10 or device in (0xC0, 0xE0))
        assert cmd in (0x01, 0x04)  # allow only read commands
        assert start >= 0 and count > 0 and start + count <= 0xFFFF
        frame: bytearray = bytearray([device, cmd])
        frame += int.to_bytes(start, 2, byteorder="big")
        frame += int.to_bytes(count * (0x10 if cmd == 0x1 else 0x1), 2, byteorder="big")
        frame += int.to_bytes(crc_modbus(frame), 2, byteorder="little")
        return bytes(frame)

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        for block in BMS.QUERY.values():
            await self._await_reply(BMS._cmd(0x0, *block))

        data: BMSsample = BMS._decode_data(
            BMS._FIELDS, self._data_final, offset=BMS.HEAD_LEN
        )

        self._pack_count = min(data.get("pack_count", 0), 0x10)

        for pack in range(1, 1 + self._pack_count):
            for block in BMS.PQUERY.values():
                await self._await_reply(self._cmd(pack, *block))

            for key, idx, sign, func in BMS._PFIELDS:
                data.setdefault(key, []).append(
                    func(
                        int.from_bytes(
                            self._data_final[pack << 8 | BMS.PIA_LEN][
                                BMS.HEAD_LEN + idx : BMS.HEAD_LEN + idx + 2
                            ],
                            byteorder="big",
                            signed=sign,
                        )
                    )
                )

            pack_cells: list[float] = BMS._cell_voltages(
                self._data_final[pack << 8 | BMS.PIB_LEN], cells=16, start=BMS.HEAD_LEN
            )
            # update per pack delta voltage
            data["delta_voltage"] = max(
                data.get("delta_voltage", 0),
                round(max(pack_cells) - min(pack_cells), 3),
            )
            # add individual cell voltages
            data.setdefault("cell_voltages", []).extend(pack_cells)
            # add temperature sensors (4x cell temperature + 4 reserved)
            data.setdefault("temp_values", []).extend(
                BMS._temp_values(
                    self._data_final[pack << 8 | BMS.PIB_LEN],
                    values=4,
                    start=BMS._TEMP_START,
                    signed=False,
                    offset=2731,
                    divider=10,
                )
            )

        self._data_final.clear()

        return data
