"""Module to support Seplos V3 Smart BMS."""

from collections.abc import Callable
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from custom_components.bms_ble.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_CURRENT,
    ATTR_CYCLE_CAP,
    ATTR_CYCLE_CHRG,
    ATTR_CYCLES,
    ATTR_DELTA_VOLTAGE,
    ATTR_POWER,
    ATTR_RUNTIME,
    ATTR_TEMPERATURE,
    ATTR_VOLTAGE,
    KEY_CELL_VOLTAGE,
    KEY_PACK,
    KEY_PACK_COUNT,
    KEY_PROBLEM,
    KEY_TEMP_VALUE,
)

from .basebms import BaseBMS, BMSsample, crc_modbus


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
    TEMP_START: Final[int] = HEAD_LEN + 32
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
    _FIELDS: Final[
        list[tuple[str, int, int, int, bool, Callable[[int], int | float]]]
    ] = [
        (ATTR_TEMPERATURE, EIB_LEN, 20, 2, False, lambda x: float(x / 10)),
        (ATTR_VOLTAGE, EIA_LEN, 0, 4, False, lambda x: float(BMS._swap32(x) / 100)),
        (
            ATTR_CURRENT,
            EIA_LEN,
            4,
            4,
            True,
            lambda x: float((BMS._swap32(x, True)) / 10),
        ),
        (ATTR_CYCLE_CHRG, EIA_LEN, 8, 4, False, lambda x: float(BMS._swap32(x) / 100)),
        (KEY_PACK_COUNT, EIA_LEN, 44, 2, False, lambda x: x),
        (ATTR_CYCLES, EIA_LEN, 46, 2, False, lambda x: x),
        (ATTR_BATTERY_LEVEL, EIA_LEN, 48, 2, False, lambda x: float(x / 10)),
        (KEY_PROBLEM, EIC_LEN, 1, 9, False, lambda x: x & 0xFFFF00FF00FF0000FF),
    ]  # Protocol Seplos V3
    _PFIELDS: Final[list[tuple[str, int, bool, Callable[[int], int | float]]]] = [
        (ATTR_VOLTAGE, 0, False, lambda x: float(x / 100)),
        (ATTR_CURRENT, 2, True, lambda x: float(x / 100)),
        (ATTR_BATTERY_LEVEL, 10, False, lambda x: float(x / 10)),
        (ATTR_CYCLES, 14, False, lambda x: x),
    ]  # Protocol Seplos V3
    _CMDS: Final[set[int]] = {field[2] for field in QUERY.values()} | {
        field[2] for field in PQUERY.values()
    }

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: dict[int, bytearray] = {}
        self._pack_count: int = 0  # number of battery packs
        self._pkglen: int = 0  # expected packet length

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
            for pattern in ("SP0*", "SP1*", "SP4*", "SP5*", "SP6*")
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
    def _calc_values() -> set[str]:
        return {ATTR_POWER, ATTR_BATTERY_CHARGING, ATTR_CYCLE_CAP, ATTR_RUNTIME}

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
            self._log.debug("RX error: %X", int(data[2]))
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
            # self._data_final[int(self._data[0])] = bytearray()  # reset invalid data
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

        self._data_final[int(self._data[0]) << 8 | int(self._data[2])] = self._data
        self._data = bytearray()
        self._data_event.set()

    async def _init_connection(self) -> None:
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
    def _cmd(device: int, cmd: int, start: int, count: int) -> bytearray:
        """Assemble a Seplos BMS command."""
        assert device >= 0x00 and (device <= 0x10 or device in (0xC0, 0xE0))
        assert cmd in (0x01, 0x04)  # allow only read commands
        assert start >= 0 and count > 0 and start + count <= 0xFFFF
        frame: bytearray = bytearray([device, cmd])
        frame += bytearray(int.to_bytes(start, 2, byteorder="big"))
        frame += bytearray(
            int.to_bytes(count * (0x10 if cmd == 0x1 else 0x1), 2, byteorder="big")
        )
        frame += bytearray(int.to_bytes(crc_modbus(frame), 2, byteorder="little"))
        return frame

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        for block in BMS.QUERY.values():
            await self._await_reply(BMS._cmd(0x0, *block))

        data: BMSsample = {
            key: func(
                int.from_bytes(
                    self._data_final[msg * 2][
                        BMS.HEAD_LEN + idx : BMS.HEAD_LEN + idx + size
                    ],
                    byteorder="big",
                    signed=sign,
                )
            )
            for key, msg, idx, size, sign, func in BMS._FIELDS
        }
        self._pack_count = min(int(data.get(KEY_PACK_COUNT, 0)), 0x10)

        for pack in range(1, 1 + self._pack_count):
            for block in BMS.PQUERY.values():
                await self._await_reply(self._cmd(pack, *block))

            data |= {
                f"{KEY_PACK}_{key}#{pack-1}": func(
                    int.from_bytes(
                        self._data_final[pack << 8 | BMS.PIA_LEN * 2][
                            BMS.HEAD_LEN + idx : BMS.HEAD_LEN + idx + 2
                        ],
                        byteorder="big",
                        signed=sign,
                    )
                )
                for key, idx, sign, func in BMS._PFIELDS
            }

            # get cell voltages
            pack_cells: list[float] = [
                float(
                    int.from_bytes(
                        self._data_final[pack << 8 | BMS.PIB_LEN * 2][
                            BMS.HEAD_LEN + idx * 2 : BMS.HEAD_LEN + idx * 2 + 2
                        ],
                        byteorder="big",
                    )
                    / 1000
                )
                for idx in range(16)
            ]
            # update per pack delta voltage
            data |= {
                ATTR_DELTA_VOLTAGE: max(
                    float(data.get(ATTR_DELTA_VOLTAGE, 0)),
                    round(max(pack_cells) - min(pack_cells), 3),
                )
            }
            # add individual cell voltages
            data |= {
                f"{KEY_CELL_VOLTAGE}{idx+16*(pack-1)}": pack_cells[idx]
                for idx in range(16)
            }
            # add temperature sensors (4x cell temperature + 4 reserved)
            data |= {
                f"{KEY_TEMP_VALUE}{idx+8*(pack-1)}": (
                    int.from_bytes(
                        self._data_final[pack << 8 | BMS.PIB_LEN * 2][
                            BMS.TEMP_START + idx * 2 : BMS.TEMP_START + idx * 2 + 2
                        ],
                        byteorder="big",
                    )
                    - 2731.5
                )
                / 10
                for idx in range(4)
            }

        self._data_final.clear()

        return data
