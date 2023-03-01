import asyncio
from threading import Thread
from time import sleep
from typing import Callable, Union

from bleak import BleakClient

from .bms import BmsSample, DeviceInfo
from .util import get_logger




class BtBms():
    def __init__(self, address: str, name, keep_alive=False, verbose_log=False):
        if address.startswith('test_'):
            from bmslib.dummy import BleakDummyClient
            self.client = BleakDummyClient(address, disconnected_callback=self._on_disconnect)
        else:
            self.client = BleakClient(address, disconnected_callback=self._on_disconnect)
        self.name = name
        self.keep_alive = keep_alive
        self.logger = get_logger(verbose_log)

    def _on_disconnect(self, client):
        if self.keep_alive:
            self.logger.warning('BMS %s disconnected!', self.__str__())

    async def connect(self, timeout=20):
        """
        Establish a BLE connection
        :param timeout:
        :return:
        """
        await self.client.connect(timeout=timeout)

    async def _connect_with_scanner(self, timeout=20):
        """
        Starts a bluetooth discovery and tries to establish a BLE connection with back off.
         This fixes connection errors for some BMS (jikong). Use instead of connect().

        :param timeout:
        :return:
        """
        import bleak
        scanner = bleak.BleakScanner()
        self.logger.debug("starting scan")
        await scanner.start()

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                if self.client.address not in discovered:
                    raise Exception('Device %s not discovered (%s)' % (self.client.address, discovered))

                self.logger.debug("connect attempt %d", attempt)
                await self.connect(timeout=timeout)
                break
            except Exception as e:
                await self.client.disconnect()
                if attempt < 8:
                    self.logger.debug('retry after error %s', e)
                    await asyncio.sleep(0.2 * (1.5 ** attempt))
                    attempt += 1
                else:
                    await scanner.stop()
                    raise

        await scanner.stop()

    async def disconnect(self):
        await self.client.disconnect()

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

    async def fetch_voltages(self):
        """
        Get cell voltages in mV. The implementation can require a prior fetch(), depending on BMS BLE data frame design.
        So the caller must call fetch() prior to fetch_voltages()
        :return: List[int]
        """
        raise NotImplementedError()

    async def fetch_temperatures(self):
        """
        Get temperature readings in °C. The implementation can require a prior fetch(), depending on BMS BLE data frame design.
        So the caller must call fetch() prior to fetch_temperatures()
        :return:
        """
        raise NotImplementedError()

    async def set_switch(self, switch: str, state: bool):
        """
        Send a switch command to the BMS to control a physical switch, usually a MOSFET or relay.
        :param switch:
        :param state:
        :return:
        """
        raise NotImplementedError()

    def __str__(self):
        return f'{self.__class__.__name__}({self.client.address})'

    async def __aenter__(self):
        # print("enter")
        if self.keep_alive and self.client.is_connected:
            return
        await self.connect()

    async def __aexit__(self, *args):
        # print("exit")
        if self.keep_alive:
            return
        await self.disconnect()

    def __await__(self):
        return self.__aexit__().__await__()

    def set_keep_alive(self, keep):
        if keep:
            self.logger.info("BMS %s keep alive enabled", self.__str__())
        self.keep_alive = keep

    def debug_data(self):
        return None
