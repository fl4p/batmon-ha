import asyncio
import fcntl
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import time
import uuid
from typing import Callable, List, Optional, Union

import backoff
import bleak.exc
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from . import FuturesPool
from .bms import BmsSample, DeviceInfo
from .util import get_logger
from .wired import SerialServiceStub, SerialCharStub

BleakDeviceNotFoundError = getattr(bleak.exc, 'BleakDeviceNotFoundError', bleak.exc.BleakError)

BleakCharacteristicNotFoundError = getattr(bleak.exc, 'BleakCharacteristicNotFoundError', None)

CharSpec = Union[BleakGATTCharacteristic, int, str, uuid.UUID]

ConnectLock = asyncio.Lock()

try:
    from bleak_retry_connector import BleakNotFoundError
except ImportError:
    class BleakNotFoundError(Exception):
        pass


@backoff.on_exception(backoff.expo, Exception, max_time=10, logger=get_logger())
async def bt_discovery(logger, timeout: int = 5, adapter=None):
    ad = adapter or 'default'
    logger.info('BT Discovery (%d seconds, adapter=%s):', timeout, adapter or 'default')
    scanner = BleakScanner(adapter=adapter) if adapter else BleakScanner()
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
        if hasattr(scanner, 'discovered_devices_and_advertisement_data'):
            devices = scanner.discovered_devices_and_advertisement_data
            addr_len = (max(len(d.address) for d, a in devices.values()) + 1) if devices else 20
            if not devices:
                logger.info(' - no devices found - ')
            else:
                logger.info("%s %*s %26s %4s", ad, addr_len, 'addr', 'name', 'rssi')
            for d, a in sorted(devices.values(), key=lambda t: t[0].address):
                logger.info("%s %*s %26s %4s", ad, addr_len, d.address, d.name, a.rssi)
            return [d for d, a in devices.values()]
        else:
            devices = scanner.discovered_devices
            if not devices:
                logger.info(' - no devices found - ')
            else:
                logger.info("BT %18s %26s", 'addr', 'name')
            for d in devices:
                logger.info("BT %s %26s", d.address, d.name)
            return devices
    finally:
        await scanner.stop()


async def bt_diagnostics(address: str, adapter: Optional[str], logger, timeout: float = 3.0) -> dict:
    """Quick scan + adapter snapshot to attach to a connect/sampling error.

    Returns {address, rssi, name, seen, others, adapter, adapters}; also logs a
    one-line summary so the error context is visible without a verbose_log dive.
    """
    ad = adapter or 'default'
    adapters = bt_adapters_info()
    target = (address or '').upper()
    result = dict(address=address, rssi=None, name=None, seen=False,
                  others=0, adapter=ad, adapters=adapters)

    try:
        scanner = BleakScanner(adapter=adapter) if adapter else BleakScanner()
    except Exception as e:
        logger.warning('bt_diagnostics: scanner init failed (%s); adapter=%s adapters=%s',
                       str(e) or type(e).__name__, ad, adapters)
        return result

    try:
        await scanner.start()
    except Exception as e:
        logger.warning('bt_diagnostics: scanner start failed (%s); adapter=%s adapters=%s',
                       str(e) or type(e).__name__, ad, adapters)
        return result

    try:
        await asyncio.sleep(timeout)
        if hasattr(scanner, 'discovered_devices_and_advertisement_data'):
            devices = scanner.discovered_devices_and_advertisement_data
            result['others'] = len(devices)
            for d, a in devices.values():
                if d.address and d.address.upper() == target:
                    result['seen'] = True
                    result['rssi'] = a.rssi
                    result['name'] = d.name
                    break
        else:
            devices = scanner.discovered_devices
            result['others'] = len(devices)
            for d in devices:
                if d.address and d.address.upper() == target:
                    result['seen'] = True
                    result['name'] = d.name
                    break
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass

    if result['seen']:
        logger.info('bt_diagnostics %s: seen rssi=%s name=%r adapter=%s (%d devices in range)',
                    address, result['rssi'], result['name'], ad, result['others'])
    else:
        logger.info('bt_diagnostics %s: NOT seen during %.1fs scan on adapter=%s (%d other devices in range, adapters=%s)',
                    address, timeout, ad, result['others'], adapters)
    return result


def bleak_version() -> str:
    try:
        import bleak
        return bleak.__version__
    except AttributeError:
        from importlib.metadata import version
        return str(version('bleak'))


def bt_stack_version():
    # noinspection PyPep8
    # When the `bleak` shadow (bumble-bleak) is active there is no BlueZ in the
    # path, so report the bumble stack instead of shelling out to bluetoothctl.
    mod = BleakClient.__module__
    if mod.startswith('bumble_bleak'):
        try:
            import bumble
            return 'bumble-v%s' % bumble.__version__
        except Exception:
            return 'bumble (%s)' % BleakClient.__name__
    # ble_stack=esphome monkey-patches BleakClient to habluetooth's wrapper.
    # No local BlueZ to report — surface the proxy stack version instead.
    if mod.startswith('habluetooth'):
        try:
            import habluetooth
            return 'esphome-proxy/habluetooth-v%s' % habluetooth.__version__
        except Exception:
            return 'esphome-proxy (%s)' % BleakClient.__name__
    try:
        # get BlueZ version
        p = subprocess.Popen(["bluetoothctl", "--version"], stdout=subprocess.PIPE)
        out, _ = p.communicate()
        s = re.search(b"(\\d+).(\\d+)", out.strip(b"'"))
        bluez_version = tuple(map(int, s.groups()))
        ver = 'bluez-v%i.%i' % bluez_version
        # bluek (ble_stack: bluek) talks to this same kernel BlueZ stack over
        # sockets, so the BlueZ version is meaningful — just tag it.
        return ('bluek/' + ver) if mod.startswith('bluek') else ver
    except:
        # get_platform_client_backend_type
        return '? (%s)' % BleakClient.__name__


def _run_cmd(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode != 0:
        print(p, out, err)
        raise Exception('error with cmd %s: %s' % (cmd, bytes.decode(err or out, 'utf-8')))
    return out


def bt_controllers():
    # Prefer bluetoothctl (gives MAC + friendly name), but fall back to the
    # kernel's /sys list when BlueZ isn't available (e.g. the bumble-bleak stack
    # owns the adapter via an HCI socket and bluetoothd is stopped).
    try:
        controllers = []
        for lin in _run_cmd(["bluetoothctl", "list"]).splitlines(keepends=False):
            s = lin.decode('utf-8').split()
            controllers.append((s[1], ' '.join(s[2:])))
        return controllers
    except Exception as e:
        logging.debug('bluetoothctl list unavailable (%s), using /sys/class/bluetooth', e)
        return [(hci, hci) for hci in bt_controllers_hci()]

def bt_controllers_hci():
    try:
        # /sys/class/bluetooth also lists per-connection child nodes (e.g.
        # "hci0:16" for an active LE connection); keep only real controllers
        # ("hci0", "hci1", ...) so callers don't try to scan a connection node.
        return [n for n in os.listdir('/sys/class/bluetooth')
                if re.fullmatch(r'hci\d+', n)]
    except:
        return []


_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
_HCIGETDEVINFO = 0x800448D3  # _IOR('H', 211, int)
# struct hci_dev_info.type low nibble -> bus name (matches `hciconfig`)
_HCI_BUS = {0: 'VIRTUAL', 1: 'USB', 2: 'PCCARD', 3: 'UART', 4: 'RS232',
            5: 'PCI', 6: 'SDIO', 7: 'SPI', 8: 'I2C', 9: 'SMD'}


def _hci_dev_info(dev_id: int) -> Optional[dict]:
    """Query controller `dev_id` via the HCIGETDEVINFO ioctl.

    Returns {index, name, mac, bus} or None if the controller doesn't exist.
    Uses the ioctl rather than /sys/class/bluetooth/hciN/* (sysfs is sparse on
    some kernels, e.g. the Raspberry Pi has no `address`). Needs CAP_NET_RAW.

    struct hci_dev_info layout: dev_id u16 @0, name[8] @2, bdaddr @10, flags @16,
    type u8 @20 (low nibble = bus).
    """
    if not sys.platform.startswith('linux'):
        return None
    if not hasattr(socket, 'AF_BLUETOOTH'):
        socket.AF_BLUETOOTH = 31  # type: ignore[attr-defined]
    if not hasattr(socket, 'BTPROTO_HCI'):
        socket.BTPROTO_HCI = 1  # type: ignore[attr-defined]
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    except OSError:
        return None
    try:
        buf = bytearray(96)
        struct.pack_into('H', buf, 0, dev_id)
        try:
            fcntl.ioctl(sock.fileno(), _HCIGETDEVINFO, buf)
        except OSError:
            return None
        name = buf[2:10].split(b'\x00', 1)[0].decode('ascii', 'replace') or ('hci%d' % dev_id)
        mac = ':'.join('%02X' % b for b in reversed(buf[10:16]))
        bus = _HCI_BUS.get(buf[20] & 0x0F, 'unknown')
        return dict(index=dev_id, name=name, mac=mac, bus=bus)
    finally:
        sock.close()


def _hci_addr_for_index(dev_id: int) -> Optional[str]:
    """Uppercase MAC of controller `dev_id`, or None."""
    info = _hci_dev_info(dev_id)
    return info['mac'] if info else None


def _hci_candidate_indices() -> List[int]:
    indices = set(range(8))
    for n in bt_controllers_hci():
        indices.add(int(n[3:]))
    return sorted(indices)


def bt_adapters_info() -> List[dict]:
    """List every present controller as {index, name, mac, bus} (USB/UART/...)."""
    out = []
    for i in _hci_candidate_indices():
        info = _hci_dev_info(i)
        if info:
            out.append(info)
    return out


def _hci_index_for_mac(mac: str) -> Optional[int]:
    """Resolve a controller MAC to its current hci index, or None if not found.

    Re-resolved on demand so the index can change (USB re-enumeration) without
    breaking a MAC-based `adapter:` setting.
    """
    target = mac.upper()
    for dev_id in _hci_candidate_indices():
        if _hci_addr_for_index(dev_id) == target:
            return dev_id
    return None


def normalize_adapter(adapter):
    """Resolve an `adapter:` value to the controller name the BLE stacks use.

    A controller MAC (e.g. "0C:EF:15:47:4A:46") is resolved to its current
    `hciN` via the HCIGETDEVINFO ioctl, for *every* stack: this gives one
    canonical name to display, scan and connect with, and lets a MAC dedupe
    against the same controller's `hciN` in the discovery sweep. (A MAC in config
    is still convenient — a stable identity that re-resolves at startup if the
    index moved.) Non-MAC values (None, "hciN", a serial path) pass through.
    """
    if not adapter or not _MAC_RE.match(adapter):
        return adapter
    index = _hci_index_for_mac(adapter)
    if index is None:
        logging.warning('adapter %s: no Bluetooth controller with that MAC found, '
                        'using as-is', adapter)
        return adapter
    hci = 'hci%d' % index
    logging.info('adapter %s resolved to %s', adapter, hci)
    return hci


def bt_power(on):
    # sudo rfkill block bluetooth
    # sudo rfkill unblock bluetooth
    # sudo systemctl start bluetooth
    # Best-effort: this drives BlueZ via bluetoothctl. With the bumble-bleak
    # stack the adapter is powered by bumble itself, and bluetoothctl is absent,
    # so never let failures here crash the caller.
    if BleakClient.__module__.startswith('bumble_bleak'):
        logging.debug('bt_power(%s) skipped: bumble-bleak manages adapter power', on)
        return
    # ble_stack=esphome has no local adapter at all — power-cycling a remote
    # ESP32 over the network would be nonsense. (Memory note: bluek is
    # intentionally NOT included here; only bumble and esphome bypass.)
    if BleakClient.__module__.startswith('habluetooth'):
        logging.debug('bt_power(%s) skipped: esphome proxy stack has no local adapter', on)
        return
    try:
        for addr, name in bt_controllers():
            logging.info('Powering %s controller %s (%s)', 'on' if on else 'off', name, addr)
            try:
                _run_cmd(["bluetoothctl", "select", addr])
                _run_cmd(["bluetoothctl", "power", "on" if on else "off"])
            except Exception as e:
                logging.error('failed to set power state for controller %s (%s): %s', name, addr, e)
    except Exception as e:
        logging.error('Failed to power controllers via bluetoothctl: %s', e)


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

            if psk:
                try:
                    import bleak.backends.bluezdbus.agent
                except ImportError:
                    self.logger.warn(
                        "Installed bleak version %s has no pairing agent, pairing with a pin will likely fail! "
                        # "Disable `install_newer_bleak` option or run `pip3 -r requirements.txt`"
                        , bleak_version())

            self._adapter = normalize_adapter(adapter)

            if address == 'serial':
                from bmslib.wired import SerialBleakClientWrapper
                assert adapter, "You need to specify a serial device (adapter)"
                self.client = SerialBleakClientWrapper(
                    adapter, baudrate=getattr(self, 'BAUDRATE', 115200))
            else:
                self.client = self._create_client(address)

            self._in_disconnect = False

            """
            When the bluetooth connection is closed externally we still need to call disconnect() function to stop_notify,
            otherwise start_notify will fail on re-connect
            """
            self._pending_disconnect_call = False

    @property
    def slug(self):
        return type(self).__name__.lower()

    def _create_client(self, addr_or_device):
        kwargs = {}
        adapter = self._adapter
        if adapter:  # hci0, hci1 (BT adapter hardware)
            self.logger.info('Using adapter %s to connect to %s (%s)', adapter, self.address, self.name)
            kwargs['adapter'] = adapter
        return BleakClient(addr_or_device,
                           handle_pairing=bool(self._psk),
                           disconnected_callback=self._on_disconnect,
                           **kwargs
                           )

    @property
    def connect_time(self):
        return self._connect_time

    async def start_notify(self, char_specifier: Union[CharSpec, List[CharSpec]],
                           callback: Callable[[int, bytearray], None], **kwargs) -> CharSpec:
        """
        This function wraps BleakClient.start_notify, differences:
          * Accept a list of char_specifiers and tries them until it finds a match
          * Before subscribing it un-subscribes dangling subscriptions
        :param char_specifier:
        :param callback:
        :param kwargs:
        :return: the accepeted char_specifier
        """

        if not char_specifier:
            raise ValueError('char_specifier is required')

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

    async def stop_notify(self, char_specifier: Union[CharSpec, List[CharSpec]]):
        try:
            # only stop notify if we already discovered services
            # otherwise client.stop_notify() might try to resolve the char_specifier, even if we are not/never connected
            if self.client.services:
                await self.client.stop_notify(char_specifier)
        except bleak.BleakError:
            pass  # "Service Discovery has not been performed yet"

    def find_char(self, uuid_or_handle: Union[str, int], property_name: str, service=None) -> Union[
        None, BleakGATTCharacteristic, SerialCharStub]:
        if self.address == 'serial':
            return SerialCharStub(uuid_or_handle, property_name)
        for service in ((service,) if service else self.client.services):
            for char in service.characteristics:
                if (char.uuid == uuid_or_handle or char.handle == uuid_or_handle) and property_name in char.properties:
                    return char if char.__hash__ else char.uuid
        return None

    def get_service(self, uuid):
        if self.address == 'serial':
            return SerialServiceStub(uuid)
        for s in self.client.services:
            if s.uuid.startswith(uuid):
                return s
        raise RuntimeError("service %s not found (have %s)", uuid, list(s.uuid for s in self.client.services))

    def _on_disconnect(self, _client):
        if self.keep_alive and self._connect_time:
            self.logger.warning('BMS %s disconnected after %.1fs!', self.__str__(), time.time() - self._connect_time)

        if self._connect_time:
            self._connect_time = 0

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

        try:
            # bleak's connect timeout is buggy (on macOS), so we wrap another timeout
            await asyncio.wait_for(self.client.connect(timeout=timeout), timeout=timeout + 1)
        except getattr(bleak.exc, 'BleakDeviceNotFoundError', bleak.exc.BleakError) as exc:
            if BtBms.shutdown:
                raise
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
                self.logger.error("Pairing %s failed!", self)

    @property
    def is_connected(self):
        return self.client and self.client.is_connected

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

        scanner = BleakScanner(adapter=self._adapter) if self._adapter else BleakScanner()
        self.logger.debug("starting scan")
        await scanner.start()

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                ad = f' using adapter {self._adapter}' if self._adapter else ''
                if self.client.address not in discovered:
                    raise BleakDeviceNotFoundError(
                        self.client.address, 'Device %s%s not discovered. Make sure it in range and is not being '
                                             'accessed by another app. (found %s)' % (
                                                 self.client.address, ad, discovered))

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
        async with ConnectLock:
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
        if hasattr(client, 'get_services'):
            services = await client.get_services()
        else:
            raise
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
