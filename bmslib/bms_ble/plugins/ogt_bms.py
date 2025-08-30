"""Module to support Offgridtec Smart Pro BMS."""

from collections.abc import Callable
from string import digits, hexdigits
from typing import Any, Final, NamedTuple

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from .basebms import AdvertisementPattern, BaseBMS, BMSsample, BMSvalue


class BMS(BaseBMS):
    """Offgridtec LiFePO4 Smart Pro type A and type B BMS implementation."""

    _IDX_NAME: Final = 0
    _IDX_LEN: Final = 1
    _IDX_FCT: Final = 2
    # magic crypt sequence of length 16
    _CRYPT_SEQ: Final[list[int]] = [2, 5, 4, 3, 1, 4, 1, 6, 8, 3, 7, 2, 5, 8, 9, 3]

    class _Response(NamedTuple):
        valid: bool
        reg: int
        value: int

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        super().__init__(ble_device, reconnect)
        self._type: str = (
            self.name[9]
            if len(self.name) >= 10 and set(self.name[10:]).issubset(digits)
            else "?"
        )
        self._key: int = (
            sum(BMS._CRYPT_SEQ[int(c, 16)] for c in (f"{int(self.name[10:]):0>4X}"))
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
        self._exp_reply: int = 0x0
        self._response: BMS._Response = BMS._Response(False, 0, 0)
        self._REGISTERS: dict[int, tuple[BMSvalue, int, Callable[[int], Any]]]
        if self._type == "A":
            self._REGISTERS = {
                # SOC (State of Charge)
                2: ("battery_level", 1, lambda x: x),
                4: ("cycle_charge", 3, lambda x: x / 1000),
                8: ("voltage", 2, lambda x: x / 1000),
                # MOS temperature
                12: ("temperature", 2, lambda x: round(x * 0.1 - 273.15, 1)),
                # 3rd byte of current is 0 (should be 1 as for B version)
                16: ("current", 3, lambda x: x / 100),
                24: ("runtime", 2, lambda x: x * 60),
                44: ("cycles", 2, lambda x: x),
                # Type A batteries have no cell voltage registers
            }
            self._HEADER = "+RAA"
        elif self._type == "B":
            self._REGISTERS = {
                # MOS temperature
                8: ("temperature", 2, lambda x: round(x * 0.1 - 273.15, 1)),
                9: ("voltage", 2, lambda x: x / 1000),
                10: ("current", 3, lambda x: x / 1000),
                # SOC (State of Charge)
                13: ("battery_level", 1, lambda x: x),
                15: ("cycle_charge", 3, lambda x: x / 1000),
                18: ("runtime", 2, lambda x: x * 60),
                23: ("cycles", 2, lambda x: x),
            }
            # add cell voltage registers, note: need to be last!
            self._HEADER = "+R16"
        else:
            self._REGISTERS = {}
            self._log.exception("unkown device type '%c'", self._type)

    @staticmethod
    def matcher_dict_list() -> list[AdvertisementPattern]:
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
    def _calc_values() -> frozenset[BMSvalue]:
        return frozenset(
            {"cycle_capacity", "power", "battery_charging", "delta_voltage"}
        )

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        self._log.debug("RX BLE data: %s", data)

        self._response = self._ogt_response(data)

        # check that descrambled message is valid
        if not self._response.valid:
            self._log.debug("response data is invalid")
            return

        if self._response.reg not in (-1, self._exp_reply):
            self._log.debug("wrong register response")
            return

        self._exp_reply = -1
        self._data_event.set()

    def _ogt_response(self, resp: bytearray) -> _Response:
        """Descramble a response from the BMS."""

        try:
            msg: Final[str] = bytearray(
                (resp[x] ^ self._key) for x in range(len(resp))
            ).decode(encoding="ascii")
        except UnicodeDecodeError:
            return BMS._Response(False, -1, 0)

        self._log.debug("response: %s", msg.rstrip("\r\n"))
        # verify correct response
        if len(msg) < 8 or not msg.startswith("+RD,"):
            return BMS._Response(False, -1, 0)
        if msg[4:7] == "Err":
            return BMS._Response(True, -1, 0)
        if not msg.endswith("\r\n") or not all(c in hexdigits for c in msg[4:-2]):
            return BMS._Response(False, -1, 0)

        # 16-bit value in network order (plus optional multiplier for 24-bit values)
        # multiplier has 1 as minimum due to current value in A type battery
        signed: bool = len(msg) > 12
        value: int = int.from_bytes(
            bytes.fromhex(msg[6:10]), byteorder="little", signed=signed
        ) * (max(int(msg[10:12], 16), 1) if signed else 1)
        return BMS._Response(True, int(msg[4:6], 16), value)

    def _ogt_command(self, reg: int, length: int) -> bytes:
        """Put together an scambled query to the BMS."""

        cmd: Final[str] = f"{self._HEADER}{reg:0>2X}{length:0>2X}"
        self._log.debug("command: %s", cmd)

        return bytes(ord(cmd[i]) ^ self._key for i in range(len(cmd)))

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""
        result: BMSsample = {}

        for reg in list(self._REGISTERS):
            self._exp_reply = reg
            await self._await_reply(
                data=self._ogt_command(reg, self._REGISTERS[reg][BMS._IDX_LEN])
            )
            if self._response.reg < 0:
                raise TimeoutError

            name, _length, func = self._REGISTERS[self._response.reg]
            result[name] = func(self._response.value)
            self._log.debug(
                "decoded data: reg: %s (#%i), raw: %i, value: %f",
                name,
                reg,
                self._response.value,
                result.get(name),
            )

        # read cell voltages for type B battery
        if self._type == "B":
            for cell_reg in range(16):
                self._exp_reply = 63 - cell_reg
                await self._await_reply(data=self._ogt_command(63 - cell_reg, 2))
                if self._response.reg < 0:
                    self._log.debug("cell count: %i", cell_reg)
                    break
                result.setdefault("cell_voltages", []).append(
                    self._response.value / 1000
                )

        # remove remaining runtime if battery is charging
        if result.get("runtime") == 0xFFFF * 60:
            del result["runtime"]

        return result
