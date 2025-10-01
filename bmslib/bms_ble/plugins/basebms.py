"""Base class defintion for battery management systems (BMS)."""
import asyncio
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, MutableMapping
from enum import IntEnum
from functools import lru_cache
from statistics import fmean
from typing import Any, Final, Literal, NamedTuple, TypedDict

from bleak import BleakClient, normalize_uuid_str
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

# from bleak_retry_connector import establish_connection
BLEAK_TIMEOUT = 20.0


async def establish_connection(client_class,
                               device,
                               name,
                               disconnected_callback,
                               services):
    assert name == device.address
    client = client_class(device, disconnected_callback, services=services)
    await client.connect()
    return client

_HRS_TO_SECS = 60 * 60  # 1 hr = 3600 seconds

# from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
# from homeassistant.components.bluetooth.match import ble_device_matches
# from homeassistant.loader import BluetoothMatcherOptional

class BluetoothMatcherOptional(TypedDict, total=False):
    """Matcher for the bluetooth integration for optional fields."""

    local_name: str
    service_uuid: str
    service_data_uuid: str
    manufacturer_id: int
    manufacturer_data_start: list[int]
    connectable: bool

BMSvalue = Literal[
    "battery_charging",
    "battery_mode",
    "battery_level",
    "current",
    "power",
    "temperature",
    "voltage",
    "cycles",
    "cycle_capacity",
    "cycle_charge",
    "delta_voltage",
    "problem",
    "runtime",
    "balance_current",
    "cell_count",
    "cell_voltages",
    "design_capacity",
    "pack_count",
    "temp_sensors",
    "temp_values",
    "problem_code",
]

BMSpackvalue = Literal[
    "pack_voltages",
    "pack_currents",
    "pack_battery_levels",
    "pack_cycles",
]


class BMSmode(IntEnum):
    """Enumeration of BMS modes."""

    UNKNOWN = -1
    BULK = 0x00
    ABSORPTION = 0x01
    FLOAT = 0x02


class BMSsample(TypedDict, total=False):
    """Dictionary representing a sample of battery management system (BMS) data."""

    battery_charging: bool  # True: battery charging
    battery_mode: BMSmode  # BMS charging mode
    battery_level: int | float  # [%]
    current: float  # [A] (positive: charging)
    power: float  # [W] (positive: charging)
    temperature: int | float  # [°C]
    voltage: float  # [V]
    cycle_capacity: int | float  # [Wh]
    cycles: int  # [#]
    delta_voltage: float  # [V]
    problem: bool  # True: problem detected
    runtime: int  # [s]
    # detailed information
    balance_current: float  # [A]
    cell_count: int  # [#]
    cell_voltages: list[float]  # [V]
    cycle_charge: int | float  # [Ah]
    design_capacity: int  # [Ah]
    pack_count: int  # [#]
    temp_sensors: int  # [#]
    temp_values: list[int | float]  # [°C]
    problem_code: int  # BMS specific code, 0 no problem, max. 64bit
    # battery pack data
    pack_voltages: list[float]  # [V]
    pack_currents: list[float]  # [A]
    pack_battery_levels: list[int | float]  # [%]
    pack_cycles: list[int]  # [#]


class BMSdp(NamedTuple):
    """Representation of one BMS data point."""

    key: BMSvalue  # the key of the value to be parsed
    pos: int  # position within the message
    size: int  # size in bytes
    signed: bool  # signed value
    fct: Callable[[int], Any] = lambda x: x  # conversion function (default do nothing)
    idx: int = -1  # array index containing the message to be parsed


class AdvertisementPattern(TypedDict, total=False):
    """Optional patterns that can match Bleak advertisement data."""

    local_name: str  # name pattern that supports Unix shell-style wildcards
    service_uuid: str  # 128-bit UUID that the device must advertise
    service_data_uuid: str  # service data for the service UUID
    manufacturer_id: int  # required manufacturer ID
    manufacturer_data_start: list[int]  # required starting bytes of manufacturer data
    connectable: bool  # True if active connections to the device are required


CALLBACK: Final = "callback"
DOMAIN: Final = "domain"
ADDRESS: Final = "address"
CONNECTABLE: Final = "connectable"
LOCAL_NAME: Final = "local_name"
SERVICE_UUID: Final = "service_uuid"
SERVICE_DATA_UUID: Final = "service_data_uuid"
MANUFACTURER_ID: Final = "manufacturer_id"
MANUFACTURER_DATA_START: Final = "manufacturer_data_start"

from fnmatch import translate


@lru_cache(maxsize=4096, typed=True)
def _compile_fnmatch(pattern: str) -> re.Pattern:
    """Compile a fnmatch pattern."""
    return re.compile(translate(pattern))


@lru_cache(maxsize=1024, typed=True)
def _memorized_fnmatch(name: str, pattern: str) -> bool:
    """Memorized version of fnmatch that has a larger lru_cache.

    The default version of fnmatch only has a lru_cache of 256 entries.
    With many devices we quickly reach that limit and end up compiling
    the same pattern over and over again.

    Bluetooth has its own memorized fnmatch with its own lru_cache
    since the data is going to be relatively the same
    since the devices will not change frequently.
    """
    return bool(_compile_fnmatch(pattern).match(name))


def ble_device_matches(
        matcher,
        service_info,
) -> bool:
    """Check if a ble device and advertisement_data matches the matcher."""
    # Don't check address here since all callers already
    # check the address and we don't want to double check
    # since it would result in an unreachable reject case.
    if matcher.get(CONNECTABLE, True) and not service_info.connectable:
        return False

    advertisement_data = service_info.advertisement
    if (
            service_uuid := matcher.get(SERVICE_UUID)
    ) and service_uuid not in advertisement_data.service_uuids:
        return False

    if (
            service_data_uuid := matcher.get(SERVICE_DATA_UUID)
    ) and service_data_uuid not in advertisement_data.service_data:
        return False

    if manfacturer_id := matcher.get(MANUFACTURER_ID):
        if manfacturer_id not in advertisement_data.manufacturer_data:
            return False
        if manufacturer_data_start := matcher.get(MANUFACTURER_DATA_START):
            manufacturer_data_start_bytes = bytearray(manufacturer_data_start)
            if not any(
                    manufacturer_data.startswith(manufacturer_data_start_bytes)
                    for manufacturer_data in advertisement_data.manufacturer_data.values()
            ):
                return False

    if (local_name := matcher.get(LOCAL_NAME)) and (
            (device_name := advertisement_data.local_name or service_info.device.name)
            is None
            or not _memorized_fnmatch(
        device_name,
        local_name,
    )
    ):
        return False

    return True


class BaseBMS(ABC):
    """Abstract base class for battery management system."""

    MAX_RETRY: Final[int] = 3  # max number of retries for data requests
    TIMEOUT: Final[float] = BLEAK_TIMEOUT / 4  # default timeout for BMS operations
    # calculate time between retries to complete all retries (2 modes) in TIMEOUT seconds
    _RETRY_TIMEOUT: Final[float] = TIMEOUT / (2 ** MAX_RETRY - 1)
    _MAX_TIMEOUT_FACTOR: Final[int] = 8  # limit timout increase to 8x
    _MAX_CELL_VOLT: Final[float] = 5.906  # max cell potential
    _HRS_TO_SECS: Final[int] = 60 * 60  # seconds in an hour

    class PrefixAdapter(logging.LoggerAdapter):
        """Logging adpater to add instance ID to each log message."""

        def process(
                self, msg: str, kwargs: MutableMapping[str, Any]
        ) -> tuple[str, MutableMapping[str, Any]]:
            """Process the logging message."""
            prefix: str = str(self.extra.get("prefix") if self.extra else "")
            return (f"{prefix} {msg}", kwargs)

    def __init__(
            self,
            ble_device: BLEDevice,
            reconnect: bool = False,
            logger_name: str = "",
    ) -> None:
        """Intialize the BMS.

        notification_handler: the callback function used for notifications from 'uuid_rx()'
            characteristic. Not defined as abstract in this base class, as it can be both,
            a normal or async function

        Args:
            logger_name (str): name of the logger for the BMS instance (usually file name)
            ble_device (BLEDevice): the Bleak device to connect to
            reconnect (bool): if true, the connection will be closed after each update

        """
        assert (
                getattr(self, "_notification_handler", None) is not None
        ), "BMS class must define _notification_handler method"
        self._ble_device: Final[BLEDevice] = ble_device
        self._reconnect: Final[bool] = reconnect
        self.name: Final[str] = self._ble_device.name or "undefined"
        self._inv_wr_mode: bool | None = None  # invert write mode (WNR <-> W)
        logger_name = logger_name or self.__class__.__module__
        self._log: Final[BaseBMS.PrefixAdapter] = BaseBMS.PrefixAdapter(
            logging.getLogger(f"{logger_name.replace('.plugins', '')}"),
            {"prefix": f"{self.name}|{self._ble_device.address[-5:].replace(':', '')}:"},
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
    def matcher_dict_list() -> list[AdvertisementPattern]:
        """Return a list of Bluetooth advertisement matchers."""

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
    def supported(cls, discovery_info) -> bool:
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
    def _calc_values() -> frozenset[BMSvalue]:
        """Return values that the BMS cannot provide and need to be calculated.

        See _add_missing_values() function for the required input to actually do so.
        """
        return frozenset()

    @staticmethod
    def _add_missing_values(data: BMSsample, values: frozenset[BMSvalue]) -> None:
        """Calculate missing BMS values from existing ones.

        Args:
            data: data dictionary with values received from BMS
            values: list of values to calculate and add to the dictionary

        Returns:
            None

        """
        if not values or not data:
            return

        def can_calc(value: BMSvalue, using: frozenset[BMSvalue]) -> bool:
            """Check value to add does not exist, is requested, and needed data is available."""
            return (value in values) and (value not in data) and using.issubset(data)

        cell_voltages: Final[list[float]] = data.get("cell_voltages", [])
        battery_level: Final[int | float] = data.get("battery_level", 0)
        current: Final[float] = data.get("current", 0)

        calculations: dict[BMSvalue, tuple[set[BMSvalue], Callable[[], Any]]] = {
            "voltage": ({"cell_voltages"}, lambda: round(sum(cell_voltages), 3)),
            "delta_voltage": (
                {"cell_voltages"},
                lambda: (
                    round(max(cell_voltages) - min(cell_voltages), 3)
                    if len(cell_voltages)
                    else None
                ),
            ),
            "cycle_charge": (
                {"design_capacity", "battery_level"},
                lambda: (data.get("design_capacity", 0) * battery_level) / 100,
            ),
            "battery_level": (
                {"design_capacity", "cycle_charge"},
                lambda: round(
                    data.get("cycle_charge", 0) / data.get("design_capacity", 0) * 100,
                    1,
                ),
            ),
            "cycle_capacity": (
                {"voltage", "cycle_charge"},
                lambda: round(data.get("voltage", 0) * data.get("cycle_charge", 0), 3),
            ),
            "power": (
                {"voltage", "current"},
                lambda: round(data.get("voltage", 0) * current, 3),
            ),
            "battery_charging": ({"current"}, lambda: current > 0),
            "runtime": (
                {"current", "cycle_charge"},
                lambda: (
                    int(
                        data.get("cycle_charge", 0)
                        / abs(current)
                        * BaseBMS._HRS_TO_SECS
                    )
                    if current < 0
                    else None
                ),
            ),
            "temperature": (
                {"temp_values"},
                lambda: (
                    round(fmean(data.get("temp_values", [])), 3)
                    if data.get("temp_values")
                    else None
                ),
            ),
        }

        for attr, (required, calc_func) in calculations.items():
            if (
                    can_calc(attr, frozenset(required))
                    and (value := calc_func()) is not None
            ):
                data[attr] = value

        # do sanity check on values to set problem state
        data["problem"] = any(
            [
                data.get("problem", False),
                data.get("problem_code", False),
                data.get("voltage") is not None and data.get("voltage", 0) <= 0,
                any(v <= 0 or v > BaseBMS._MAX_CELL_VOLT for v in cell_voltages),
                data.get("delta_voltage", 0) > BaseBMS._MAX_CELL_VOLT,
                data.get("cycle_charge") is not None
                and data.get("cycle_charge", 0.0) <= 0.0,
                battery_level > 100,
            ]
        )

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Disconnect callback function."""

        self._log.debug("disconnected from BMS")

    async def _init_connection(
            self, char_notify: BleakGATTCharacteristic | int | str | None = None
    ) -> None:
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

        try:
            await self._client.disconnect()  # ensure no stale connection exists
        except (BleakError, TimeoutError) as exc:
            self._log.debug(
                "failed to disconnect stale connection (%s)", type(exc).__name__
            )

        self._log.debug("connecting BMS")
        self._client = await establish_connection(
            client_class=BleakClient,
            device=self._ble_device,
            name=self._ble_device.address,
            disconnected_callback=self._on_disconnect,
            services=[*self.uuid_services()],
        )

        try:
            await self._init_connection()
        except Exception as exc:
            self._log.info(
                "failed to initialize BMS connection (%s)", type(exc).__name__
            )
            await self.disconnect()
            raise

    def _wr_response(self, char: int | str) -> bool:
        char_tx: Final[BleakGATTCharacteristic | None] = (
            self._client.services.get_characteristic(char)
        )
        return bool(char_tx and "write" in getattr(char_tx, "properties", []))

    async def _send_msg(
            self,
            data: bytes,
            max_size: int,
            char: int | str,
            attempt: int,
            inv_wr_mode: bool = False,
    ) -> None:
        """Send message to the bms in chunks if needed."""
        chunk_size: Final[int] = max_size or len(data)

        for i in range(0, len(data), chunk_size):
            chunk: bytes = data[i: i + chunk_size]
            self._log.debug(
                "TX BLE req #%i (%s%s%s): %s",
                attempt + 1,
                "!" if inv_wr_mode else "",
                "W" if self._wr_response(char) else "WNR",
                "." if self._inv_wr_mode is not None else "",
                chunk.hex(" "),
            )
            await self._client.write_gatt_char(
                normalize_uuid_str(char),
                chunk,
                response=(self._wr_response(char) != inv_wr_mode),
            )

    async def _await_reply(
            self,
            data: bytes,
            char: int | str | None = None,
            wait_for_notify: bool = True,
            max_size: int = 0,
    ) -> None:
        """Send data to the BMS and wait for valid reply notification."""

        for inv_wr_mode in (
                [False, True] if self._inv_wr_mode is None else [self._inv_wr_mode]
        ):
            try:
                self._data_event.clear()  # clear event before requesting new data
                for attempt in range(BaseBMS.MAX_RETRY):
                    await self._send_msg(
                        data, max_size, char or self.uuid_tx(), attempt, inv_wr_mode
                    )
                    if not wait_for_notify:
                        return  # write without wait for response selected
                    try:
                        await asyncio.wait_for(
                            self._wait_event(),
                            BaseBMS._RETRY_TIMEOUT
                            * min(2 ** attempt, BaseBMS._MAX_TIMEOUT_FACTOR),
                        )
                    except TimeoutError:
                        self._log.debug("TX BLE request timed out.")
                        continue  # retry sending data

                    self._inv_wr_mode = inv_wr_mode
                    return  # leave loop if no exception
            except BleakError as exc:
                # reconnect on communication errors
                self._log.warning(
                    "TX BLE request error, retrying connection (%s)", type(exc).__name__
                )
                await self.disconnect()
                await self._connect()
        raise TimeoutError

    async def disconnect(self, reset: bool = False) -> None:
        """Disconnect the BMS, includes stoping notifications."""

        self._log.debug("disconnecting BMS (%s)", str(self._client.is_connected))
        try:
            self._data_event.clear()
            if reset:
                self._inv_wr_mode = None  # reset write mode
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
        """Retrieve updated values from the BMS using method of the subclass.

        Args:
            raw (bool): if true, the raw data from the BMS is returned without
                any calculations or missing values added

        Returns:
            BMSsample: dictionary with BMS values

        """
        await self._connect()

        data: BMSsample = await self._async_update()
        self._add_missing_values(data, self._calc_values())

        if self._reconnect:
            # disconnect after data update to force reconnect next time (slow!)
            await self.disconnect()

        return data

    @staticmethod
    def _decode_data(
            fields: tuple[BMSdp, ...],
            data: bytearray | dict[int, bytearray],
            *,
            byteorder: Literal["little", "big"] = "big",
            offset: int = 0,
    ) -> BMSsample:
        result: BMSsample = {}
        for field in fields:
            if isinstance(data, dict) and field.idx not in data:
                continue
            msg: bytearray = data[field.idx] if isinstance(data, dict) else data
            result[field.key] = field.fct(
                int.from_bytes(
                    msg[offset + field.pos: offset + field.pos + field.size],
                    byteorder=byteorder,
                    signed=field.signed,
                )
            )
        return result

    @staticmethod
    def _cell_voltages(
            data: bytearray,
            *,
            cells: int,
            start: int,
            size: int = 2,
            byteorder: Literal["little", "big"] = "big",
            divider: int = 1000,
    ) -> list[float]:
        """Return cell voltages from BMS message.

        Args:
            data: Raw data from BMS
            cells: Number of cells to read
            start: Start position in data array
            size: Number of bytes per cell value (defaults 2)
            byteorder: Byte order ("big"/"little" endian)
            divider: Value to divide raw value by, defaults to 1000 (mv to V)

        Returns:
            list[float]: List of cell voltages in volts

        """
        return [
            value / divider
            for idx in range(cells)
            if (len(data) >= start + (idx + 1) * size)
            and (
                value := int.from_bytes(
                    data[start + idx * size: start + (idx + 1) * size],
                    byteorder=byteorder,
                    signed=False,
                )
            )
        ]

    @staticmethod
    def _temp_values(
            data: bytearray,
            *,
            values: int,
            start: int,
            size: int = 2,
            byteorder: Literal["little", "big"] = "big",
            signed: bool = True,
            offset: float = 0,
            divider: int = 1,
    ) -> list[int | float]:
        """Return temperature values from BMS message.

        Args:
            data: Raw data from BMS
            values: Number of values to read
            start: Start position in data array
            size: Number of bytes per cell value (defaults 2)
            byteorder: Byte order ("big"/"little" endian)
            signed: Indicates whether two's complement is used to represent the integer.
            offset: The offset read values are shifted by (for Kelvin use 273.15)
            divider: Value to divide raw value by, defaults to 1000 (mv to V)

        Returns:
            list[int | float]: List of temperature values

        """
        return [
            value / divider if divider != 1 else value
            for idx in range(values)
            if (len(data) >= start + (idx + 1) * size)
               and (
                   value := int.from_bytes(
                       data[start + idx * size: start + (idx + 1) * size],
                       byteorder=byteorder,
                       signed=signed,
                   )
                            - offset
               )
        ]


def crc_modbus(data: bytearray) -> int:
    """Calculate CRC-16-CCITT MODBUS."""
    crc: int = 0xFFFF
    for i in data:
        crc ^= i & 0xFF
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc % 2 else (crc >> 1)
    return crc & 0xFFFF


def lrc_modbus(data: bytearray) -> int:
    """Calculate MODBUS LRC."""
    return ((sum(data) ^ 0xFFFF) + 1) & 0xFFFF


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


def crc_sum(frame: bytearray, size: int = 1) -> int:
    """Calculate the checksum of a frame using a specified size.

    size : int, optional
        The size of the checksum in bytes (default is 1).
    """
    return sum(frame) & ((1 << (8 * size)) - 1)
