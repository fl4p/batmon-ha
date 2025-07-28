import asyncio
import math
import time
from typing import Dict, Tuple

from bleak import BLEDevice
from bmslib.bt import ProxyBleakScanner

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bms_ble.const import ATTR_BATTERY_LEVEL, KEY_CELL_COUNT
from bmslib.bt import BtBms


class BLEDeviceResolver:
    devices: Dict[Tuple[str, str], BLEDevice] = {}

    @staticmethod
    async def resolve(addr: str, adapter=None) -> BLEDevice:
        key = (adapter, addr)
        if key in BLEDeviceResolver.devices:
            return BLEDeviceResolver.devices[key]

        if BtBms.shutdown:
            raise RuntimeError("in shutdown")

        import bleak
        scanner_kw = {}
        if adapter and str(adapter).startswith("proxy:"):
            proxy_addr = str(adapter).split(":",1)[1]
            scanner = ProxyBleakScanner(proxy_addr)
        else:
            if adapter:
                scanner_kw['adapter'] = adapter
            scanner = bleak.BleakScanner(**scanner_kw)

        await scanner.start()

        t0 = time.time()
        while time.time() - t0 < 5:
            if BtBms.shutdown:
                raise RuntimeError("in shutdown")

            try:
                for d in scanner.discovered_devices:
                    BLEDeviceResolver.devices[(adapter, d.address)] = d
                    BLEDeviceResolver.devices[(adapter, d.name)] = d
                if key in BLEDeviceResolver.devices:
                    break
            except Exception as e:
                pass

            await asyncio.sleep(.1)

        await scanner.stop()
        return BLEDeviceResolver.devices.get(key, None)


class BMS():

    def __init__(self, address,  type, module=None, keep_alive=False, adapter=None, name=None, **kwargs):
        self.address = address
        self.adapter = adapter
        self.name = name
        self._type = type
        self._blebms_module = module
        self._keep_alive = keep_alive

        self._last_sample = None

        self.is_virtual = False
        self.verbose_log = False

        self.connect_time = time.time()

        import bmslib.bms_ble.plugins.basebms

        self.ble_bms: bmslib.bms_ble.plugins.basebms.BaseBMS = None

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
            await self.connect()

    async def __aexit__(self, *args):
        if not self._keep_alive and self.is_connected:
            await self.disconnect()

    def __await__(self):
        return self.__aexit__().__await__()

    async def connect(self, timeout=20, **kwargs):
        import bmslib.bms_ble.plugins.basebms

        ble_device = await BLEDeviceResolver.resolve(self.address, adapter=self.adapter)

        if ble_device is None:
            raise RuntimeError("device %s not found" % self.address)


        self.ble_bms: bmslib.bms_ble.plugins.basebms.BaseBMS = self._blebms_module.BMS(
            ble_device=ble_device,
            reconnect=not self._keep_alive
        )

        await self.ble_bms._connect()

        # await super().connect(**kwargs)
        # try:
        #    await super().connect(timeout=6)
        # except Exception as e:
        #    self.logger.info("%s normal connect failed (%s), connecting with scanner", self.name, str(e) or type(e))
        #    await self._connect_with_scanner(timeout=timeout)
        # await self.start_notify(self.CHAR_UUID, self._notification_handler)

    async def disconnect(self):
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
        from bmslib.bms_ble.const import (
            ATTR_CURRENT,
            ATTR_CYCLE_CAP,
            ATTR_CYCLE_CHRG,
            ATTR_POWER,
            ATTR_TEMPERATURE,
            ATTR_VOLTAGE,
            ATTR_CYCLES,
            ATTR_BALANCE_CUR,
        )

        sample = await self.ble_bms.async_update()
        self._last_sample = sample
        return BmsSample(
            soc=sample[ATTR_BATTERY_LEVEL],
            voltage=sample[ATTR_VOLTAGE],
            current=sample[ATTR_CURRENT], power=sample.get(ATTR_POWER, math.nan),
            capacity=sample.get(ATTR_CYCLE_CHRG, math.nan),  # todo ?
            cycle_capacity=sample.get(ATTR_CYCLE_CAP, math.nan),  # todo ?
            num_cycles=sample.get(ATTR_CYCLES, math.nan),
            balance_current=sample.get(ATTR_BALANCE_CUR, math.nan),
            temperatures=[sample.get(ATTR_TEMPERATURE)],  # todo?
            # mos_temperature=

        )

    async def fetch_voltages(self):
        s = self._last_sample
        if s is None:
            return []
        v = [self._last_sample[f'cell#{i}'] for i in range(s[KEY_CELL_COUNT])]
        return v

    def debug_data(self):
        return self._last_sample
