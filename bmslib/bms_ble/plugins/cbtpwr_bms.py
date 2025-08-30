"""Module to support CBT Power Smart BMS."""

from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

#from homeassistant.util.unit_conversion import _HRS_TO_SECS

from .basebms import AdvertisementPattern, BaseBMS, BMSdp, BMSsample, BMSvalue, crc_sum, _HRS_TO_SECS


class BMS(BaseBMS):
    """CBT Power Smart BMS class implementation."""

    HEAD: Final[bytes] = bytes([0xAA, 0x55])
    TAIL_RX: Final[bytes] = bytes([0x0D, 0x0A])
    TAIL_TX: Final[bytes] = bytes([0x0A, 0x0D])
    MIN_FRAME: Final[int] = len(HEAD) + len(TAIL_RX) + 3  # CMD, LEN, CRC, 1 Byte each
    CRC_POS: Final[int] = -len(TAIL_RX) - 1
    LEN_POS: Final[int] = 3
    CMD_POS: Final[int] = 2
    CELL_VOLTAGE_CMDS: Final[list[int]] = [0x5, 0x6, 0x7, 0x8]
    _FIELDS: Final[tuple[BMSdp, ...]] = (
        BMSdp("voltage", 4, 4, False, lambda x: x / 1000, 0x0B),
        BMSdp("current", 8, 4, True, lambda x: x / 1000, 0x0B),
        BMSdp("temperature", 4, 2, True, lambda x: x, 0x09),
        BMSdp("battery_level", 4, 1, False, lambda x: x, 0x0A),
        BMSdp("design_capacity", 4, 2, False, lambda x: x, 0x15),
        BMSdp("cycles", 6, 2, False, lambda x: x, 0x15),
        BMSdp("runtime", 14, 2, False, lambda x: x * _HRS_TO_SECS / 100, 0x0C),
        BMSdp("problem_code", 4, 4, False, lambda x: x, 0x21),
    )
    _CMDS: Final[list[int]] = list({field.idx for field in _FIELDS})

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(ble_device, reconnect)

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
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
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {
                "power",
                "battery_charging",
                "delta_voltage",
                "cycle_capacity",
                "temperature",
            }
        )

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
    def _cmd(cmd: bytes, value: list[int] | None = None) -> bytes:
        """Assemble a CBT Power BMS command."""
        value = [] if value is None else value
        assert len(value) <= 255
        frame = bytearray([*BMS.HEAD, cmd[0], len(value), *value])
        frame.append(crc_sum(frame[len(BMS.HEAD) :]))
        frame.extend(BMS.TAIL_TX)
        return bytes(frame)

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        resp_cache: dict[int, bytearray] = {}  # avoid multiple queries
        for cmd in BMS._CMDS:
            self._log.debug("request command 0x%X.", cmd)
            try:
                await self._await_reply(BMS._cmd(cmd.to_bytes(1)))
            except TimeoutError:
                continue
            if cmd != self._data[BMS.CMD_POS]:
                self._log.debug(
                    "incorrect response 0x%X to command 0x%X",
                    self._data[BMS.CMD_POS],
                    cmd,
                )
            resp_cache[self._data[BMS.CMD_POS]] = self._data.copy()

        voltages: list[float] = []
        for cmd in BMS.CELL_VOLTAGE_CMDS:
            try:
                await self._await_reply(BMS._cmd(cmd.to_bytes(1)))
            except TimeoutError:
                break
            cells: list[float] = BMS._cell_voltages(
                self._data, cells=5, start=4, byteorder="little"
            )
            voltages.extend(cells)
            if len(voltages) % 5 or len(cells) == 0:
                break

        data: BMSsample = BMS._decode_data(BMS._FIELDS, resp_cache, byteorder="little")

        # get cycle charge from design capacity and SoC
        if data.get("design_capacity") and data.get("battery_level"):
            data["cycle_charge"] = (
                data.get("design_capacity", 0) * data.get("battery_level", 0) / 100
            )
        # remove runtime if not discharging
        if data.get("current", 0) >= 0:
            data.pop("runtime", None)

        return data | {"cell_voltages": voltages}
