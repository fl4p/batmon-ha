"""Base class defintion for battery management systems (BMS)."""

from abc import ABCMeta, abstractmethod
import asyncio
import logging
from statistics import fmean
from typing import Final, Literal, Dict, Union

from bleak import BleakClient, normalize_uuid_str
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
#from bleak_retry_connector import establish_connection

from bmslib.bms_ble.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_CURRENT,
    ATTR_CYCLE_CAP,
    ATTR_CYCLE_CHRG,
    ATTR_DELTA_VOLTAGE,
    ATTR_POWER,
    ATTR_PROBLEM,
    ATTR_RUNTIME,
    ATTR_TEMPERATURE,
    ATTR_VOLTAGE,
    KEY_CELL_VOLTAGE,
    KEY_DESIGN_CAP,
    KEY_PROBLEM,
    KEY_TEMP_VALUE,
)

class BluetoothServiceInfoBleak():
    pass

def ble_device_matches(*args, **kwargs):
    pass

class BluetoothMatcherOptional():
    def __init__(self, **kwargs):
        pass

_HRS_TO_SECS = 3600

#from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
#from homeassistant.components.bluetooth.match import ble_device_matches
#from homeassistant.loader import BluetoothMatcherOptional
#from homeassistant.util.unit_conversion import _HRS_TO_SECS

#type BMSsample = dict[str, int | float | bool]
BMSsample = Dict[str, Union[int, float, bool]]


class BaseBMS(metaclass=ABCMeta):
    """Base class for battery management system."""

    TIMEOUT = 10
    MAX_CELL_VOLTAGE: Final[float] = 5.906  # max cell potential

    def __init__(
        self,
        logger_name: str,
        ble_device: BLEDevice,
        reconnect: bool = False,
    ) -> None:
        """Intialize the BMS.

        logger_name: name of the logger for the BMS instance (usually file name)
        notification_handler: the callback used for notifications from 'uuid_rx()' characteristics
        ble_device: the Bleak device to connect to
        reconnect: if true, the connection will be closed after each update
        """
        assert (
            getattr(self, "_notification_handler", None) is not None
        ), "BMS class must define _notification_handler method"
        self._ble_device: Final[BLEDevice] = ble_device
        self._reconnect: Final[bool] = reconnect
        self.name: Final[str] = self._ble_device.name or "undefined"
        self._log: Final[logging.Logger] = logging.getLogger(
            f"{logger_name.replace('.plugins', '')}::{self.name}:"
            f"{self._ble_device.address[-5:].replace(':','')})"
        )

        self._log.debug(
            "initializing %s, BT address: %s", self.device_id(), ble_device.address
        )
        self._client: BleakClient = BleakClient(
            self._ble_device,
            disconnected_callback=self._on_disconnect,
            services=[*self.uuid_services()],
        )
        self._data: bytearray = bytearray()
        self._data_event: Final[asyncio.Event] = asyncio.Event()

    @staticmethod
    @abstractmethod
    def matcher_dict_list() -> list[dict]:
        """Return a list of Bluetooth matchers."""

    @staticmethod
    @abstractmethod
    def device_info() -> dict[str, str]:
        """Return a dictionary of device information.

        keys: manufacturer, model
        """

    @classmethod
    def device_id(cls) -> str:
        """Return device information as string."""
        return " ".join(cls.device_info().values())

    @classmethod
    def supported(cls, discovery_info: BluetoothServiceInfoBleak) -> bool:
        """Return true if service_info matches BMS type."""
        for matcher_dict in cls.matcher_dict_list():
            if ble_device_matches(
                BluetoothMatcherOptional(**matcher_dict), discovery_info
            ):
                return True
        return False

    @staticmethod
    @abstractmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""

    @staticmethod
    @abstractmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""

    @staticmethod
    @abstractmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""

    @staticmethod
    def _calc_values() -> frozenset[str]:
        """Return values that the BMS cannot provide and need to be calculated.

        See calc_values() function for the required input to actually do so.
        """
        return frozenset()

    @staticmethod
    def _add_missing_values(data: BMSsample, values: frozenset[str]) -> None:
        """Calculate missing BMS values from existing ones.

        data: data dictionary from BMS
        values: list of values to add to the dictionary
        """
        if not values or not data:
            return

        def can_calc(value: str, using: frozenset[str]) -> bool:
            """Check value to add does not exist, is requested, and needed data is available."""
            return (value in values) and (value not in data) and using.issubset(data)

        # calculate total voltage (sum of all cell voltages)
        cell_voltages: list[float]
        if can_calc(ATTR_VOLTAGE, frozenset({f"{KEY_CELL_VOLTAGE}0"})):
            cell_voltages = [
                float(v) for k, v in data.items() if k.startswith(KEY_CELL_VOLTAGE)
            ]
            data[ATTR_VOLTAGE] = round(sum(cell_voltages), 3)

        # calculate delta voltage (maximum cell voltage difference)
        if can_calc(ATTR_DELTA_VOLTAGE, frozenset({f"{KEY_CELL_VOLTAGE}1"})):
            cell_voltages = [
                float(v) for k, v in data.items() if k.startswith(KEY_CELL_VOLTAGE)
            ]
            data[ATTR_DELTA_VOLTAGE] = round(max(cell_voltages) - min(cell_voltages), 3)

        # calculate cycle charge from design capacity and SoC
        if can_calc(ATTR_CYCLE_CHRG, frozenset({KEY_DESIGN_CAP, ATTR_BATTERY_LEVEL})):
            data[ATTR_CYCLE_CHRG] = (
                data[KEY_DESIGN_CAP] * data[ATTR_BATTERY_LEVEL]
            ) / 100

        # calculate cycle capacity from voltage and cycle charge
        if can_calc(ATTR_CYCLE_CAP, frozenset({ATTR_VOLTAGE, ATTR_CYCLE_CHRG})):
            data[ATTR_CYCLE_CAP] = round(data[ATTR_VOLTAGE] * data[ATTR_CYCLE_CHRG], 3)

        # calculate current power from voltage and current
        if can_calc(ATTR_POWER, frozenset({ATTR_VOLTAGE, ATTR_CURRENT})):
            data[ATTR_POWER] = round(data[ATTR_VOLTAGE] * data[ATTR_CURRENT], 3)

        # calculate charge indicator from current
        if can_calc(ATTR_BATTERY_CHARGING, frozenset({ATTR_CURRENT})):
            data[ATTR_BATTERY_CHARGING] = data[ATTR_CURRENT] > 0

        # calculate runtime from current and cycle charge
        if (
            can_calc(ATTR_RUNTIME, frozenset({ATTR_CURRENT, ATTR_CYCLE_CHRG}))
            and data[ATTR_CURRENT] < 0
        ):
            data[ATTR_RUNTIME] = int(
                data[ATTR_CYCLE_CHRG] / abs(data[ATTR_CURRENT]) * _HRS_TO_SECS
            )
        # calculate temperature (average of all sensors)
        if can_calc(ATTR_TEMPERATURE, frozenset({f"{KEY_TEMP_VALUE}0"})):
            data[ATTR_TEMPERATURE] = round(
                fmean([v for k, v in data.items() if k.startswith(KEY_TEMP_VALUE)]),
                3,
            )

        # do sanity check on values to set problem state
        data[ATTR_PROBLEM] = (
            data.get(ATTR_PROBLEM, False)
            or bool(data.get(KEY_PROBLEM, False))
            or (
                data.get(ATTR_VOLTAGE, 1) <= 0
                or any(
                    v <= 0 or v > BaseBMS.MAX_CELL_VOLTAGE
                    for k, v in data.items()
                    if k.startswith(KEY_CELL_VOLTAGE)
                )
                or data.get(ATTR_DELTA_VOLTAGE, 0) > BaseBMS.MAX_CELL_VOLTAGE
                or data.get(ATTR_CYCLE_CHRG, 1) <= 0
                or data.get(ATTR_BATTERY_LEVEL, 0) > 100
            )
        )

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Disconnect callback function."""

        self._log.debug("disconnected from BMS")

    async def _init_connection(self) -> None:
        # reset any stale data from BMS
        self._data.clear()
        self._data_event.clear()

        await self._client.start_notify(
            normalize_uuid_str(self.uuid_rx()), getattr(self, "_notification_handler")
        )

    async def _connect(self) -> None:
        """Connect to the BMS and setup notification if not connected."""

        if self._client.is_connected:
            self._log.debug("BMS already connected")
            return

        self._log.debug("connecting BMS")
        self._client = BleakClient(self._ble_device, self._on_disconnect,
                                   services=[*self.uuid_services()])
        await self._client.connect()
        #self._client = await establish_connection(
        #    client_class=BleakClient,
       #     device=self._ble_device,
       #     name=self._ble_device.address,
        #    disconnected_callback=self._on_disconnect,
        #    services=[*self.uuid_services()],
        #)

        try:
            await self._init_connection()
        except Exception as err:
            self._log.info(
                "failed to initialize BMS connection (%s)", type(err).__name__
            )
            await self.disconnect()
            raise

    async def _await_reply(
        self,
        data: bytes,
        char: BleakGATTCharacteristic | int | str | None = None,
        wait_for_notify: bool = True,
    ) -> None:
        """Send data to the BMS and wait for valid reply notification."""

        self._log.debug("TX BLE data: %s", data.hex(" "))
        self._data_event.clear()  # clear event before requesting new data
        await self._client.write_gatt_char(normalize_uuid_str(char or self.uuid_tx()), data)
        if wait_for_notify:
            await asyncio.wait_for(self._wait_event(), timeout=self.TIMEOUT)

    async def disconnect(self) -> None:
        """Disconnect the BMS, includes stoping notifications."""

        if self._client.is_connected:
            self._log.debug("disconnecting BMS")
            try:
                self._data_event.clear()
                await self._client.disconnect()
            except BleakError:
                self._log.warning("disconnect failed!")

    async def _wait_event(self) -> None:
        """Wait for data event and clear it."""
        await self._data_event.wait()
        self._data_event.clear()

    @abstractmethod
    async def _async_update(self) -> BMSsample:
        """Return a dictionary of BMS values (keys need to come from the SENSOR_TYPES list)."""

    async def async_update(self) -> BMSsample:
        """Retrieve updated values from the BMS using method of the subclass."""
        await self._connect()

        data = await self._async_update()

        self._add_missing_values(data, self._calc_values())

        if self._reconnect:
            # disconnect after data update to force reconnect next time (slow!)
            await self.disconnect()

        return data


def crc_modbus(data: bytearray) -> int:
    """Calculate CRC-16-CCITT MODBUS."""
    crc: int = 0xFFFF
    for i in data:
        crc ^= i & 0xFF
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc % 2 else (crc >> 1)
    return crc & 0xFFFF


def crc_xmodem(data: bytearray) -> int:
    """Calculate CRC-16-CCITT XMODEM."""
    crc: int = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if (crc & 0x8000) else (crc << 1)
    return crc & 0xFFFF


def crc8(data: bytearray) -> int:
    """Calculate CRC-8/MAXIM-DOW."""
    crc: int = 0x00  # Initialwert für CRC

    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 0x1 else crc >> 1

    return crc & 0xFF


def crc_sum(frame: bytes) -> int:
    """Calculate frame CRC."""
    return sum(frame) & 0xFF
