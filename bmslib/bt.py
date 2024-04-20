import asyncio
import re
import subprocess
import time
from typing import Callable, List, Union

import backoff
import bleak.exc
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from . import FuturesPool
from .bms import BmsSample, DeviceInfo
from .util import get_logger

BleakDeviceNotFoundError = getattr(bleak.exc, 'BleakDeviceNotFoundError', bleak.exc.BleakError)


@backoff.on_exception(backoff.expo, Exception, max_time=10, logger=None)
async def bt_discovery(logger):
    logger.info('BT Discovery:')
    devices = await BleakScanner.discover()
    if not devices:
        logger.info(' - no devices found - ')
    for d in devices:
        logger.info("BT %s %26s", d.address, d.name)
    return devices


def bleak_version() -> str:
    try:
        import bleak
        return bleak.__version__
    except AttributeError:
        from importlib.metadata import version
        return str(version('bleak'))


def bt_stack_version():
    # noinspection PyPep8
    try:
        # get BlueZ version
        p = subprocess.Popen(["bluetoothctl", "--version"], stdout=subprocess.PIPE)
        out, _ = p.communicate()
        s = re.search(b"(\\d+).(\\d+)", out.strip(b"'"))
        bluez_version = tuple(map(int, s.groups()))
        return 'bluez-v%i.%i' % bluez_version
    except:
        # get_platform_client_backend_type
        return '? (%s)' % BleakClient.__name__


def bt_power(on):
    # sudo rfkill block bluetooth

    # sudo rfkill unblock bluetooth
    # sudo systemctl start bluetooth
    cmd = ["bluetoothctl", "power", "on" if on else "off"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode != 0:
        print(p, out, err)
        raise Exception('error with cmd %s: %s' % (cmd, bytes.decode(err or out, 'utf-8')))


class BtBms:
    shutdown = False

    def __init__(self, address: str, name: str, keep_alive=False, psk=None, adapter=None, verbose_log=False,
                 _uses_pin=False):
        self.address = address
        self.name = name
        self.keep_alive = keep_alive
        self.verbose_log = verbose_log
        self.logger = get_logger(verbose_log)
        self._fetch_futures = FuturesPool()
        self._psk = psk
        self._connect_time = 0
        self._pending_disconnect_call = False

        if not _uses_pin and psk:
            self.logger.warning('%s usually does not use a pairing PIN', type(self).__name__)

        if address.startswith('test_'):
            from bmslib.models.dummy import BleakDummyClient
            self.client = BleakDummyClient(address, disconnected_callback=self._on_disconnect)
            self._adapter = "fake"
        else:
            kwargs = {}
            if psk:
                try:
                    import bleak.backends.bluezdbus.agent
                except ImportError:
                    self.logger.warn(
                        "Installed bleak version %s has no pairing agent, pairing with a pin will likely fail! "
                        "Disable `install_newer_bleak` option or run `pip3 -r requirements.txt`",
                        bleak_version())

            self._adapter = adapter
            if adapter:  # hci0, hci1 (BT adapter hardware)
                self.logger.info('Using adapter %s', adapter)
                kwargs['adapter'] = adapter

            self.client = BleakClient(address,
                                      handle_pairing=bool(psk),
                                      disconnected_callback=self._on_disconnect,
                                      **kwargs
                                      )

            self._in_disconnect = False

            """
            When the bluetooth connection is closed externally we still need to call disconnect() function to stop_notify,
            otherwise start_notify will fail on re-connect
            """
            self._pending_disconnect_call = False

    @property
    def connect_time(self):
        return self._connect_time

    async def start_notify(self, char_specifier, callback: Callable[[int, bytearray], None], **kwargs):
        """
        This function wraps BleakClient.start_notify, differences:
          * Accept a list of char_specifiers and tries them until it finds a match
          * Before subscribing it un-subscribes dangling subscriptions
        :param char_specifier:
        :param callback:
        :param kwargs:
        :return:
        """
        if not isinstance(char_specifier, list):
            char_specifier = [char_specifier]
        exception = None
        for cs in char_specifier:
            try:
                try:
                    await self.client.stop_notify(cs)  # stop any orphan notifies
                except:
                    pass
                await self.client.start_notify(cs, callback, **kwargs)
                return cs
            except Exception as e:
                exception = e
        await enumerate_services(self.client, self.logger)
        raise exception

    def find_char(self, uuid_or_handle: Union[str, int], property_name: str, service=None) -> Union[
        None, BleakGATTCharacteristic]:
        for service in ((service,) if service else self.client.services):
            for char in service.characteristics:
                if (char.uuid == uuid_or_handle or char.handle == uuid_or_handle) and property_name in char.properties:
                    return char if char.__hash__ else char.uuid
        return None

    def get_service(self, uuid):
        for s in self.client.services:
            if s.uuid.startswith(uuid):
                return s
        raise RuntimeError("service %s not found (have %s)", uuid, list(s.uuid for s in self.client.services))

    def _on_disconnect(self, _client):
        if self.keep_alive and self._connect_time:
            self.logger.warning('BMS %s disconnected after %.1fs!', self.__str__(), time.time() - self._connect_time)

        if self.is_connected:
            self.logger.warning("%s _on_disconnect but is_connected=True")

        # if not self._in_disconnect:
        #    self._pending_disconnect_call = True

        try:
            self._fetch_futures.clear()
        except Exception as e:
            self.logger.warning('error clearing futures pool: %s', str(e) or type(e))

    async def _connect_client(self, timeout):
        if BtBms.shutdown:
            raise RuntimeError("in shutdown")

        if self.verbose_log:
            self.logger.info('connecting %s (%s) adapter=%s timeout=%d', self.name, self.address,
                             self._adapter or "default", timeout)
        # bleak`s connect timeout is buggy (on macos)
        try:
            await asyncio.wait_for(self.client.connect(timeout=timeout), timeout=timeout + 1)
        except getattr(bleak.exc, 'BleakDeviceNotFoundError', bleak.exc.BleakError) as exc:
            self.logger.error("%s, starting scanner", exc)
            await bt_discovery(self.logger)
            raise

        self._connect_time = time.time()

        if self.verbose_log:
            try:
                await enumerate_services(self.client, logger=self.logger)
            except:
                pass

        if self._psk:
            def get_passkey(device: str, pin, passkey):
                if pin:
                    self.logger.info(f"Device {device} is displaying pin '{pin}'")
                    return True

                if passkey:
                    self.logger.info(f"Device {device} is displaying passkey '{passkey:06d}'")
                    return True

                self.logger.info(f"Device {device} asking for psk, giving '{self._psk}'")
                return str(self._psk) or None

            self.logger.debug("Pairing %s using psk '%s'...", self.name, self._psk)
            res = await self.client.pair(callback=get_passkey)
            if not res:
                self.logger.error("Pairing failed!")

    @property
    def is_connected(self):
        return self.client.is_connected

    @property
    def is_virtual(self):
        from bmslib.group import VirtualGroupBms
        return isinstance(self, VirtualGroupBms)

    async def connect(self, timeout=20):
        """
        Establish a BLE connection
        :param timeout:
        :return:
        """
        if self._pending_disconnect_call:
            self._pending_disconnect_call = False
            await self.disconnect()

        await self._connect_client(timeout=timeout)

    async def _connect_with_scanner(self, timeout=20):
        """
        Starts a bluetooth discovery and tries to establish a BLE connection with back off.
         This fixes connection errors for some BMS (jikong). Use instead of connect().

        :param timeout:
        :return:
        """

        if self._pending_disconnect_call:
            self._pending_disconnect_call = False
            await self.disconnect()

        if BtBms.shutdown:
            raise RuntimeError("in shutdown")

        import bleak
        scanner_kw = {}
        if self._adapter:
            scanner_kw['adapter'] = self._adapter
        scanner = bleak.BleakScanner(**scanner_kw)
        self.logger.debug("starting scan")
        await scanner.start()

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                if self.client.address not in discovered:
                    raise BleakDeviceNotFoundError(
                        self.client.address, 'Device %s not discovered. Make sure it in range and is not being '
                                             'accessed by another app. (found %s)' % (self.client.address, discovered))

                self.logger.debug("connect attempt %d", attempt)
                await self._connect_client(timeout=timeout / 2)
                break
            except Exception as e:
                await self.client.disconnect()
                if attempt < 8:
                    self.logger.debug('retry %d after error %s', attempt, e)
                    await asyncio.sleep(0.2 * (1.5 ** attempt))
                    attempt += 1
                else:
                    await scanner.stop()
                    raise

        await scanner.stop()

    async def disconnect(self):
        self._in_disconnect = True
        await self.client.disconnect()
        self._in_disconnect = False
        self._fetch_futures.clear()

    async def fetch_device_info(self) -> DeviceInfo:
        """
        Retrieve static BMS device info (HW, SW version, serial number, etc)
        :return: DeviceInfo
        """
        raise NotImplementedError()

    async def fetch(self) -> BmsSample:
        """
        Retrieve a BMS sample
        :return:
        """
        raise NotImplementedError()

    async def fetch_voltages(self) -> List[int]:
        """
        Get cell voltages in mV. The implementation can require a prior fetch(), depending on BMS BLE data frame design.
        So the caller must call fetch() prior to fetch_voltages()
        :return: List[int]
        """
        raise NotImplementedError()

    async def fetch_temperatures(self) -> List[float]:
        """
        Get temperature readings in °C. The implementation can require a prior fetch(), depending on BMS BLE data frame design.
        So the caller must call fetch() prior to fetch_temperatures()
        :return:
        """
        raise NotImplementedError()

    async def subscribe(self, callback: Callable[[BmsSample], None]):
        raise NotImplemented()

    async def subscribe_voltages(self, callback: Callable[[List[int]], None]):
        raise NotImplemented()

    async def set_switch(self, switch: str, state: bool):
        """
        Send a switch command to the BMS to control a physical switch, usually a MOSFET or relay.
        :param switch:
        :param state:
        :return:
        """
        raise NotImplementedError()

    def __str__(self):
        return f'{self.__class__.__name__}({self.client.address},{self.name})'

    async def __aenter__(self):
        # print("enter")
        if self.keep_alive and self.is_connected:
            return
        await self.connect()

    async def __aexit__(self, *args):
        # print("exit")
        if self.keep_alive:
            return
        if self.client.is_connected:
            await self.disconnect()

    def __await__(self):
        return self.__aexit__().__await__()

    def set_keep_alive(self, keep):
        if keep:
            self.logger.debug("BMS %s keep alive enabled", self.__str__())
        self.keep_alive = keep

    def debug_data(self):
        return None


# noinspection DuplicatedCode
async def enumerate_services(client: BleakClient, logger):
    try:
        # might raise bleak.exc.BleakError: Service Discovery has not been performed yet
        services = client.services
        assert services
    except:
        services = await client.get_services()
    for service in services:
        logger.info(f"[Service] {service}")
        for char in service.characteristics:
            if "read" in char.properties:
                try:
                    value = bytes(await client.read_gatt_char(char.uuid))
                    logger.info(
                        f"\t[Characteristic] {char} ({','.join(char.properties)}), Value: {value}"
                    )
                except Exception as e:
                    logger.error(
                        f"\t[Characteristic] {char} ({','.join(char.properties)}), Value: {e}"
                    )

            else:
                value = None
                logger.info(
                    f"\t[Characteristic] {char} ({','.join(char.properties)}), Value: {value}"
                )

            for descriptor in char.descriptors:
                try:
                    value = bytes(
                        await client.read_gatt_descriptor(descriptor.handle)
                    )
                    logger.info(f"\t\t[Descriptor] {descriptor}) | Value: {value}")
                except Exception as e:
                    logger.error(f"\t\t[Descriptor] {descriptor}) | Value: {e}")
