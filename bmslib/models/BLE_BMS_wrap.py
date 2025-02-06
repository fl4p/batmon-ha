import asyncio
import time
from typing import Dict, Tuple

from bleak import BLEDevice

from bmslib.bms import BmsSample, DeviceInfo
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
        if adapter:
            scanner_kw['adapter'] = adapter
        scanner = bleak.BleakScanner(**scanner_kw)

        await scanner.start()

        t0 = time.time()
        while time.time() - t0 < 5:
            if BtBms.shutdown:
                raise RuntimeError("in shutdown")

            try:
                discovered = {d.address: d for d in scanner.discovered_devices}
                for a, device in discovered.items():
                    BLEDeviceResolver.devices[(adapter, a)] = device
                if addr in discovered:
                    break
            except Exception as e:
                pass

            await asyncio.sleep(.1)

        await scanner.stop()
        return BLEDeviceResolver.devices.get(key, None)


class BMS():

    def __init__(self, address, type, keep_alive=False, adapter=None, name=None, **kwargs):
        self.address = address
        self.adapter = adapter
        self.name = name
        self._type = type
        self._keep_alive = keep_alive

        self._buffer = bytearray()
        self._switches = None
        self._last_response = None
        self._voltages = []

        self.is_virtual = False

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

        import bmslib.bms_ble.plugins.seplos_bms
        modules = dict(
            seplos=bmslib.bms_ble.plugins.seplos_bms
        )

        self.ble_bms: bmslib.bms_ble.plugins.basebms.BaseBMS = modules[self._type].BMS(
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
        sample = await self.ble_bms.async_update()

        pass

    async def fetch_voltages(self):
        return self._voltages

    def debug_data(self):
        return self._last_response
