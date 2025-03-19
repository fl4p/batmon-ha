"""Module to support CBT Power Smart BMS."""

from collections.abc import Callable
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from bmslib.bms_ble.const import (
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
    KEY_DESIGN_CAP,
    KEY_PROBLEM,
)
from homeassistant.util.unit_conversion import _HRS_TO_SECS

from .basebms import BaseBMS, BMSsample, crc_sum


class BMS(BaseBMS):
    """CBT Power Smart BMS class implementation."""

    BAT_TIMEOUT = 1
    HEAD: Final[bytes] = bytes([0xAA, 0x55])
    TAIL_RX: Final[bytes] = bytes([0x0D, 0x0A])
    TAIL_TX: Final[bytes] = bytes([0x0A, 0x0D])
    MIN_FRAME: Final[int] = len(HEAD) + len(TAIL_RX) + 3  # CMD, LEN, CRC, 1 Byte each
    CRC_POS: Final[int] = -len(TAIL_RX) - 1
    LEN_POS: Final[int] = 3
    CMD_POS: Final[int] = 2
    CELL_VOLTAGE_CMDS: Final[list[int]] = [0x5, 0x6, 0x7, 0x8]
    _FIELDS: Final[
        list[tuple[str, int, int, int, bool, Callable[[int], int | float]]]
    ] = [
        (ATTR_VOLTAGE, 0x0B, 4, 4, False, lambda x: float(x / 1000)),
        (ATTR_CURRENT, 0x0B, 8, 4, True, lambda x: float(x / 1000)),
        (ATTR_TEMPERATURE, 0x09, 4, 2, True, lambda x: x),
        (ATTR_BATTERY_LEVEL, 0x0A, 4, 1, False, lambda x: x),
        (KEY_DESIGN_CAP, 0x15, 4, 2, False, lambda x: x),
        (ATTR_CYCLES, 0x15, 6, 2, False, lambda x: x),
        (ATTR_RUNTIME, 0x0C, 14, 2, False, lambda x: float(x * _HRS_TO_SECS / 100)),
        (KEY_PROBLEM, 0x21, 4, 4, False, lambda x: x),
    ]
    _CMDS: Final[list[int]] = list({field[1] for field in _FIELDS})

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(__name__, ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {"service_uuid": BMS.uuid_services()[0], "connectable": True},
            {  # Creabest
                "service_uuid": normalize_uuid_str("fff0"),
                "manufacturer_id": 0,
                "connectable": True,
            },
            {
                "service_uuid": normalize_uuid_str("03c1"),
                "manufacturer_id": 0x5352,
                "connectable": True,
            },
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "CBT Power", "model": "Smart BMS"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of services required by BMS."""
        return [normalize_uuid_str("ffe5"), normalize_uuid_str("ffe0")]

    @staticmethod
    def uuid_rx() -> str:
        """Return characteristic that provides notification/read property."""
        return "ffe4"

    @staticmethod
    def uuid_tx() -> str:
        """Return characteristic that provides write property."""
        return "ffe9"

    @staticmethod
    def _calc_values() -> set[str]:
        return {
            ATTR_POWER,
            ATTR_BATTERY_CHARGING,
            ATTR_DELTA_VOLTAGE,
            ATTR_CYCLE_CAP,
            ATTR_TEMPERATURE,
        }

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Retrieve BMS data update."""
        self._log.debug("RX BLE data: %s", data)

        # verify that data is long enough
        if len(data) < BMS.MIN_FRAME or len(data) != BMS.MIN_FRAME + data[BMS.LEN_POS]:
            self._log.debug("incorrect frame length (%i): %s", len(data), data)
            return

        if not data.startswith(BMS.HEAD) or not data.endswith(BMS.TAIL_RX):
            self._log.debug("incorrect frame start/end: %s", data)
            return

        if (crc := crc_sum(data[len(BMS.HEAD) : len(data) + BMS.CRC_POS])) != data[
            BMS.CRC_POS
        ]:
            self._log.debug(
                "invalid checksum 0x%X != 0x%X",
                data[len(data) + BMS.CRC_POS],
                crc,
            )
            return

        self._data = data
        self._data_event.set()

    @staticmethod
    def _gen_frame(cmd: bytes, value: list[int] | None = None) -> bytes:
        """Assemble a CBT Power BMS command."""
        value = [] if value is None else value
        assert len(value) <= 255
        frame = bytes([*BMS.HEAD, cmd[0]])
        frame += bytes([len(value), *value])
        frame += bytes([crc_sum(frame[len(BMS.HEAD) :])])
        frame += bytes([*BMS.TAIL_TX])
        return frame

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        """Return cell voltages from status message."""
        return {
            f"{KEY_CELL_VOLTAGE}{idx+(data[BMS.CMD_POS]-5)*5}": int.from_bytes(
                data[4 + 2 * idx : 6 + 2 * idx],
                byteorder="little",
                signed=True,
            )
            / 1000
            for idx in range(5)
        }

    @staticmethod
    def _decode_data(cache: dict[int, bytearray]) -> BMSsample:
        data: BMSsample = {}
        for field, cmd, pos, size, sign, fct in BMS._FIELDS:
            if cmd in cache:
                data[field] = fct(
                    int.from_bytes(cache[cmd][pos : pos + size], "little", signed=sign)
                )
        return data

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        resp_cache: dict[int, bytearray] = {}  # avoid multiple queries
        for cmd in BMS._CMDS:
            self._log.debug("request command 0x%X.", cmd)
            try:
                await self._await_reply(BMS._gen_frame(cmd.to_bytes(1)))
            except TimeoutError:
                continue
            if cmd != self._data[BMS.CMD_POS]:
                self._log.debug(
                    "incorrect response 0x%X to command 0x%X",
                    self._data[BMS.CMD_POS],
                    cmd,
                )
            resp_cache[self._data[BMS.CMD_POS]] = self._data.copy()

        voltages: dict[str, float] = {}
        for cmd in BMS.CELL_VOLTAGE_CMDS:
            try:
                await self._await_reply(BMS._gen_frame(cmd.to_bytes(1)))
            except TimeoutError:
                break
            voltages |= BMS._cell_voltages(self._data)
            if invalid := [k for k, v in voltages.items() if v == 0]:
                for k in invalid:
                    voltages.pop(k)
                break

        data: BMSsample = BMS._decode_data(resp_cache)

        # get cycle charge from design capacity and SoC
        if data.get(KEY_DESIGN_CAP) and data.get(ATTR_BATTERY_LEVEL):
            data[ATTR_CYCLE_CHRG] = (
                data[KEY_DESIGN_CAP] * data[ATTR_BATTERY_LEVEL] / 100
            )
        # remove runtime if not discharging
        if data.get(ATTR_CURRENT, 0) >= 0:
            data.pop(ATTR_RUNTIME, None)

        return data | voltages
