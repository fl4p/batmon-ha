"""Module to support Offgridtec Smart Pro BMS."""

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
)

from .basebms import BaseBMS, BMSsample

# magic crypt sequence of length 16
CRYPT_SEQ: Final[list[int]] = [2, 5, 4, 3, 1, 4, 1, 6, 8, 3, 7, 2, 5, 8, 9, 3]


class BMS(BaseBMS):
    """Offgridtec LiFePO4 Smart Pro type A and type B battery class implementation."""

    BAT_TIMEOUT = 1
    IDX_NAME: Final = 0
    IDX_LEN: Final = 1
    IDX_FCT: Final = 2

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(__name__, ble_device, reconnect)
        self._type: str = self.name[9] if len(self.name) >= 10 else "?"
        self._key: int = (
            sum(CRYPT_SEQ[int(c, 16)] for c in (f"{int(self.name[10:]):0>4X}"))
            if self._type in "AB"
            else 0
        ) + (5 if (self._type == "A") else 8)
        self._log.info(
            "%s type: %c, ID: %s, key: 0x%X",
            self.device_id(),
            self._type,
            self.name[10:],
            self._key,
        )
        self._values: BMSsample  # dictionary of BMS return values
        self._REGISTERS: dict[int, tuple[str, int, Callable[[int], int | float] | None]]
        if self._type == "A":
            self._REGISTERS = {
                # SOC (State of Charge)
                2: (ATTR_BATTERY_LEVEL, 1, None),
                4: (ATTR_CYCLE_CHRG, 3, lambda x: float(x) / 1000),
                8: (ATTR_VOLTAGE, 2, lambda x: float(x) / 1000),
                # MOS temperature
                12: (ATTR_TEMPERATURE, 2, lambda x: round(float(x) * 0.1 - 273.15, 1)),
                # 3rd byte of current is 0 (should be 1 as for B version)
                16: (ATTR_CURRENT, 3, lambda x: float(x) / 100),
                24: (ATTR_RUNTIME, 2, lambda x: int(x * 60)),
                44: (ATTR_CYCLES, 2, None),
                # Type A batteries have no cell voltage registers
            }
            self._HEADER = "+RAA"
        elif self._type == "B":
            self._REGISTERS = {
                # MOS temperature
                8: (ATTR_TEMPERATURE, 2, lambda x: round(float(x) * 0.1 - 273.15, 1)),
                9: (ATTR_VOLTAGE, 2, lambda x: float(x) / 1000),
                10: (ATTR_CURRENT, 3, lambda x: float(x) / 1000),
                # SOC (State of Charge)
                13: (ATTR_BATTERY_LEVEL, 1, None),
                15: (ATTR_CYCLE_CHRG, 3, lambda x: float(x) / 1000),
                18: (ATTR_RUNTIME, 2, lambda x: int(x * 60)),
                23: (ATTR_CYCLES, 2, None),
            }
            # add cell voltage registers, note: need to be last!
            self._REGISTERS |= {  # pragma: no branch
                63 - reg: (f"{KEY_CELL_VOLTAGE}{reg+1}", 2, lambda x: float(x) / 1000)
                for reg in range(16)
            }
            self._HEADER = "+R16"
        else:
            self._REGISTERS = {}
            self._log.exception("unkown device type '%c'", self._type)

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Return a list of Bluetooth matchers."""
        return [
            {
                "local_name": "SmartBat-[AB]*",
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return a dictionary of device information."""
        return {"manufacturer": "Offgridtec", "model": "LiFePo4 Smart Pro"}

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
        return "fff6"

    @staticmethod
    def _calc_values() -> set[str]:
        return {ATTR_CYCLE_CAP, ATTR_POWER, ATTR_BATTERY_CHARGING, ATTR_DELTA_VOLTAGE}

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        self._values = {}
        for reg in list(self._REGISTERS):
            try:
                await self._await_reply(data=self._ogt_command(reg))
            except TimeoutError:
                self._log.debug(
                    "reading %s timed out", self._REGISTERS[reg][BMS.IDX_NAME]
                )
            if reg > 48 and f"{KEY_CELL_VOLTAGE}{64-reg}" not in self._values:
                break

        # remove remaining runtime if battery is charging
        if self._values.get(ATTR_RUNTIME) == 0xFFFF * 60:
            del self._values[ATTR_RUNTIME]

        return self._values

    def _notification_handler(
        self, sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        self._log.debug("RX BLE data: %s", data)

        valid, reg, nat_value = self._ogt_response(data)

        # check that descrambled message is valid and from the right characteristic
        if valid and sender.uuid == normalize_uuid_str(BMS.uuid_rx()):
            name, _length, func = self._REGISTERS[reg]
            value: int | float = func(nat_value) if func else nat_value
            self._log.debug(
                "decoded data: reg: %s (#%i), raw: %i, value: %f",
                name,
                reg,
                nat_value,
                value,
            )
            self._values[name] = value
        else:
            self._log.debug("response data is invalid")
        self._data_event.set()

    def _ogt_response(self, resp: bytearray) -> tuple[bool, int, int]:
        """Descramble a response from the BMS."""

        try:
            msg: Final[str] = bytearray(
                (resp[x] ^ self._key) for x in range(len(resp))
            ).decode(encoding="ascii")
        except UnicodeDecodeError:
            return False, 0, 0

        self._log.debug("response: %s", msg[:-2])
        # verify correct response
        if msg[4:7] == "Err" or msg[:4] != "+RD," or msg[-2:] != "\r\n":
            return False, 0, 0
        # 16-bit value in network order (plus optional multiplier for 24-bit values)
        # multiplier has 1 as minimum due to current value in A type battery
        signed: bool = len(msg) > 12
        value: int = int.from_bytes(
            bytes.fromhex(msg[6:10]), byteorder="little", signed=signed
        ) * (max(int(msg[10:12], 16), 1) if signed else 1)
        return True, int(msg[4:6], 16), value

    def _ogt_command(self, command: int) -> bytes:
        """Put together an scambled query to the BMS."""

        cmd: Final[str] = (
            f"{self._HEADER}{command:0>2X}{self._REGISTERS[command][BMS.IDX_LEN]:0>2X}"
        )
        self._log.debug("command: %s", cmd)

        return bytearray(ord(cmd[i]) ^ self._key for i in range(len(cmd)))
