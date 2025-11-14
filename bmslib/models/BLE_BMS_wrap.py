import asyncio
import math
import time
from typing import Dict, Tuple, Optional

from bleak import BLEDevice

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bms_ble.plugins.basebms import BMSsample
from bmslib.bt import BtBms, BleakDeviceNotFoundError, ConnectLock
from bmslib.scan import get_shared_scanner


class BLEDeviceResolver:
    devices: Dict[Tuple[str, str], BLEDevice] = {}

    @staticmethod
    async def resolve(addr: str, adapter=None) -> BLEDevice:
        key = (adapter, addr)
        if key in BLEDeviceResolver.devices:
            return BLEDeviceResolver.devices[key]

        if BtBms.shutdown:
            raise KeyboardInterrupt("in shutdown")

        scanner = await get_shared_scanner(adapter)

        t0 = time.time()
        while time.time() - t0 < 5:
            if BtBms.shutdown:
                raise KeyboardInterrupt("in shutdown")

            try:
                for d in scanner.discovered_devices:
                    BLEDeviceResolver.devices[(adapter, d.address)] = d
                    BLEDeviceResolver.devices[(adapter, d.name)] = d
                if key in BLEDeviceResolver.devices:
                    break
            except Exception as e:
                pass

            await asyncio.sleep(.1)

        return BLEDeviceResolver.devices.get(key, None)


class BMS():

    def __init__(self, address, type, blebms_class=None, keep_alive=False, adapter=None, name=None, **kwargs):
        self.address = address
        self.adapter = adapter
        self.name = name
        self._type = type
        self._blebms_class = blebms_class
        self._keep_alive = keep_alive

        self._last_sample: Optional[BMSsample] = None

        self.is_virtual = False
        self.verbose_log = False

        self.connect_time = time.time()

        import bmslib.bms_ble.plugins.basebms

        self.ble_bms: Optional[bmslib.bms_ble.plugins.basebms.BaseBMS] = None

    @property
    def client(self):
        return self.ble_bms._client if self.ble_bms else None

    def _notification_handler(self, sender, data: bytes):
        pass

    def set_keep_alive(self, keep):
        self._keep_alive = keep
        # self.ble_bms._reconnect = not keep

    @property
    def is_connected(self):
        return self.ble_bms and self.ble_bms._client.is_connected

    async def __aenter__(self):
        if not self._keep_alive or not self.is_connected:
            async with ConnectLock:
                await self.connect()

    async def __aexit__(self, *args):
        if not self._keep_alive and self.is_connected:
            await self.disconnect()

    def __await__(self):
        return self.__aexit__().__await__()

    async def connect(self, timeout=20, **kwargs):

        ble_device = await BLEDeviceResolver.resolve(self.address, adapter=self.adapter or None)

        if ble_device is None:
            raise BleakDeviceNotFoundError(
                "device %s not found (adapter=%s)" % (self.address, self.adapter or 'default'))

        from aiobmsble.basebms import BaseBMS
        self.ble_bms: BaseBMS = self._blebms_class(
            ble_device=ble_device,
            keep_alive=self._keep_alive,
        )

        # try:
        await self.ble_bms._connect()
        # except BleakCharacteristicNotFoundError as e:
        #    from bmslib.util import get_logger
        #    logger = get_logger()
        #    from bmslib.bt import enumerate_services
        #    logger.error('%s Error: %s', self, e)
        #    await enumerate_services(self.client, logger)

        # await super().connect(**kwargs)
        # try:
        #    await super().connect(timeout=6)
        # except Exception as e:
        #    self.logger.info("%s normal connect failed (%s), connecting with scanner", self.name, str(e) or type(e))
        #    await self._connect_with_scanner(timeout=timeout)
        # await self.start_notify(self.CHAR_UUID, self._notification_handler)

    async def disconnect(self):
        if self.ble_bms is not None:
            await self.ble_bms.disconnect()
        # await self.client.stop_notify(self.CHAR_UUID)
        # await super().disconnect()

    async def fetch_device_info(self) -> DeviceInfo:
        di = self.ble_bms.device_info()
        return DeviceInfo(
            mnf=di.get("manufacturer"),
            model=di.get("model"),
            hw_version=None,
            sw_version=None,
            name=None,
            sn=None,
        )

    async def fetch(self) -> BmsSample:

        sample: BMSsample = await self.ble_bms.async_update()
        self._last_sample = sample
        return BmsSample(
            soc=sample['battery_level'],
            voltage=sample['voltage'],
            current=sample['current'],
            power=sample.get('power', math.nan),
            capacity=sample.get('cycle_charge', math.nan),  # todo ?
            cycle_capacity=sample.get('cycle_capacity', math.nan),  # todo ?
            num_cycles=sample.get('cycles', math.nan),
            balance_current=sample.get('balance_current', math.nan),
            temperatures=[sample.get('temperature')],  # todo?
            # mos_temperature=

        )

    async def fetch_voltages(self):
        # return voltages in mV
        s = self._last_sample
        if s is None:
            return []
        v = [s['cell_voltages'][i] * 1000 for i in range(s['cell_count'])]
        for i in range(len(v)):
            if v[i] == int(v[i]):
                v[i] = int(v[i])
        return v

    def debug_data(self):
        return self._last_sample
